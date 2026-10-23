"""Connect the imported BAS documents to each other.

The GL import reproduces every BAS voucher, but as separate documents:
nothing says which monthly invoice a delivery note was billed on, which
invoice a receipt paid, or which supplier document a payment settled. BAS
records the first of these (``STR10.LINKED_*``) and not the other two (its
statements are a running balance), so:

* delivery note -> invoice is copied from BAS, exactly;
* supplier credits become native vendor bills (``006`` purchases and the
  journal vouchers that credit one supplier against expenses);
* receipts and supplier payments are matched in three passes: the invoice
  the receipt names in its text, then an invoice of exactly the same
  amount, then the oldest open invoices first (Mohammed's choice,
  2026-10-21).
"""
import logging
import re
from collections import defaultdict
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Command

_logger = logging.getLogger(__name__)

_PAYABLE_PREFIX = '2101'
_VAT_INPUT_CODE = '1209010001'
_BILL_JOURNAL = ('BCBIL', 'BAS Vendor Bills')
# A 4-6 digit number in a receipt's text ("سداد فاتورة رقم 22040"). Only ever
# used against the SAME client's open invoices, so "ف 5+6+7" (months) or a
# phone number cannot pay someone else's invoice.
_INVOICE_NO_RE = re.compile(r'(?<!\d)(\d{4,6})(?!\d)')
_NOT_A_BILL_LINE = ('asset_cash', 'asset_receivable', 'liability_payable',
                    'liability_credit_card')


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    x_bas_invoice_key = fields.Char(string='BAS Invoice', index=True, copy=False,
                                    readonly=True)
    x_bas_invoice_id = fields.Many2one('account.move', string='Billed On',
                                       index=True, copy=False, readonly=True)


class AccountMove(models.Model):
    _inherit = 'account.move'

    x_bas_delivery_ids = fields.One2many('stock.picking', 'x_bas_invoice_id',
                                         string='BAS Delivery Notes')
    x_bas_delivery_count = fields.Integer(compute='_compute_bas_delivery_count')

    @api.depends('x_bas_delivery_ids')
    def _compute_bas_delivery_count(self):
        counts = dict(self.env['stock.picking']._read_group(
            [('x_bas_invoice_id', 'in', self.ids)], ['x_bas_invoice_id'], ['__count']))
        for move in self:
            move.x_bas_delivery_count = counts.get(move, 0)

    def action_view_bas_deliveries(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Delivery Notes'),
            'res_model': 'stock.picking',
            'view_mode': 'list,form',
            'domain': [('x_bas_invoice_id', '=', self.id)],
        }


class BasGlImport(models.Model):
    _inherit = 'ksw.bas.gl.import'

    # ------------------------------------------------------------------
    # Delivery note -> monthly invoice
    # ------------------------------------------------------------------
    @api.model
    def action_link_delivery_invoices(self, date_from, date_to, company=None):
        """Stamp each BAS delivery note with the invoice BAS billed it on.

        ``STR10.LINKED_FTYPE/FTYPE2/CODE2/NUMBER1`` on a ``600`` line names the
        ``002`` invoice; ~94% of credit notes carry it. Cash notes never do:
        they are paid in the till on the spot, so they stay unlinked.
        """
        conn = self._bas_connect()
        cur = conn.cursor(as_dict=True)
        cur.execute("""
            SELECT DISTINCT FTYPE, FTYPE2, RTRIM(CODE2) CODE2, CAST(NUMBER1 AS BIGINT) N,
                   LINKED_FTYPE2 LF2, RTRIM(LINKED_CODE2) LC2,
                   CAST(LINKED_NUMBER1 AS BIGINT) LN
            FROM STR10
            WHERE FTYPE = '600' AND LINKED_FTYPE = '002'
              AND FDATE >= %s AND FDATE < DATEADD(day, 1, %s)
        """, (date_from, date_to))
        pairs = {}
        for r in cur.fetchall():
            note = f"{r['FTYPE']}/{r['FTYPE2']}/{r['CODE2']}/{r['N']}"
            pairs[note] = f"002/{r['LF2']}/{r['LC2']}/{r['LN']}"
        conn.close()
        return self._link_deliveries(pairs, company)

    @api.model
    def _link_deliveries(self, pairs, company=None):
        """``pairs`` = {delivery note key: invoice key}."""
        company = company or self.env.company
        Picking = self.env['stock.picking'].with_context(tracking_disable=True)
        Move = self.env['account.move']
        by_invoice = defaultdict(list)
        for note, inv in pairs.items():
            by_invoice[inv].append(note)
        moves = {}
        keys = list(by_invoice)
        for i in range(0, len(keys), 2000):
            for m in Move.search([('x_bas_key', 'in', keys[i:i + 2000]),
                                  ('company_id', '=', company.id),
                                  ('state', '!=', 'cancel')]):
                moves[m.x_bas_key] = m
        stats = defaultdict(int)
        for n, (inv, notes) in enumerate(by_invoice.items(), 1):
            picks = Picking.search([('x_bas_key', 'in', notes)])
            stats['notes_in_bas'] += len(notes)
            stats['notes_not_in_odoo'] += len(notes) - len(picks)
            if not picks:
                continue
            move = moves.get(inv)
            todo = picks.filtered(lambda p: p.x_bas_invoice_key != inv
                                  or p.x_bas_invoice_id != (move or Move))
            if todo:
                todo.write({'x_bas_invoice_key': inv,
                            'x_bas_invoice_id': move.id if move else False})
            stats['linked' if move else 'invoice_not_in_odoo'] += len(picks)
            if n % 500 == 0:
                self.env.cr.commit()
        stats['invoices'] = len(by_invoice)
        _logger.info('BAS delivery -> invoice: %s', dict(stats))
        return dict(stats)

    # ------------------------------------------------------------------
    # Supplier credits -> vendor bills
    # ------------------------------------------------------------------
    @api.model
    def _purchase_taxes(self, company):
        """(15% posting to BAS input VAT 1209010001, 0%)."""
        T = self.env['account.tax']
        t15 = T.search([('company_id', '=', company.id), ('type_tax_use', '=', 'purchase'),
                        ('amount', '=', 15.0), ('amount_type', '=', 'percent')]).filtered(
            lambda t: t.invoice_repartition_line_ids.filtered(
                lambda r: r.repartition_type == 'tax').account_id.mapped('x_bas_code')
            == [_VAT_INPUT_CODE])[:1]
        t0 = T.search([('company_id', '=', company.id), ('type_tax_use', '=', 'purchase'),
                       ('amount', '=', 0.0), ('amount_type', '=', 'percent')], limit=1)
        return t15, t0

    @api.model
    def _bill_journal(self, company):
        code, name = _BILL_JOURNAL
        J = self.env['account.journal']
        j = J.search([('code', '=', code), ('company_id', '=', company.id)], limit=1)
        return j or J.create({'name': name, 'code': code, 'type': 'purchase',
                              'company_id': company.id})

    @api.model
    def action_convert_bills(self, date_from, date_to, company=None, limit_docs=None):
        """Replace supplier-credit entries by vendor bills (same x_bas_key).

        Ledger-neutral like ``action_convert_invoices``: one bill line per BAS
        debit row (same account, same amount, so cost centres still match), VAT
        through the purchase tax only when it reproduces BAS's VAT exactly.
        Anything that is not one supplier billed for expenses stays an entry.
        """
        company = company or self.env.company
        t15, t0 = self._purchase_taxes(company)
        accounts = {a.x_bas_code: a for a in self.env['account.account'].search(
            [('x_bas_code', '!=', False)])}
        partners = {p.x_bas_code: p for p in self.env['res.partner'].search(
            [('x_bas_code', '=like', _PAYABLE_PREFIX + '%')])}

        conn = self._bas_connect()
        cur = conn.cursor(as_dict=True)
        cur.execute("""
            SELECT FTYPE, FTYPE2, RTRIM(CODE2) CODE2, NUMBER1, SERIAL,
                   RTRIM(ISNULL(FCODE,'')) FCODE, RTRIM(ISNULL(TCODE,'')) TCODE,
                   AMOUNT, REF, FDATE
            FROM vou10 v
            WHERE FDATE >= %s AND FDATE < DATEADD(day, 1, %s)
              AND FTYPE NOT IN ('600', '001', '002', '101')
              AND EXISTS (SELECT 1 FROM vou10 s
                          WHERE s.FTYPE = v.FTYPE AND s.FTYPE2 = v.FTYPE2
                            AND s.CODE2 = v.CODE2 AND s.NUMBER1 = v.NUMBER1
                            AND LEFT(RTRIM(s.TCODE), 4) = %s)
            ORDER BY FTYPE, FTYPE2, CODE2, NUMBER1, SERIAL
        """, (date_from, date_to, _PAYABLE_PREFIX))
        vou = defaultdict(list)
        for r in cur.fetchall():
            vou[f"{r['FTYPE']}/{r['FTYPE2']}/{r['CODE2']}/{r['NUMBER1']:.0f}"].append(r)
        conn.close()

        Move = self.env['account.move'].with_context(tracking_disable=True).with_company(company)
        keys = list(vou)[:limit_docs] if limit_docs else list(vou)
        old = {}
        for i in range(0, len(keys), 2000):
            for m in Move.search([('x_bas_key', 'in', keys[i:i + 2000]),
                                  ('move_type', '=', 'entry'), ('state', '=', 'posted')]):
                old[m.x_bas_key] = m
        journal = self._bill_journal(company)
        stats, problems = defaultdict(int), []
        batch, batch_old = [], Move.browse()

        def flush():
            nonlocal batch, batch_old
            if not batch:
                return
            batch_old.button_draft()
            batch_old.with_context(force_delete=True).unlink()
            bills = Move.create(batch)
            bills._post(soft=False)
            stats['converted'] += len(bills)
            batch, batch_old = [], Move.browse()
            self.env.cr.commit()

        for key in keys:
            entry = old.get(key)
            if not entry:
                continue                   # already a bill, or never imported
            vals, why = self._build_bill(key, vou[key], accounts, partners, t15, t0,
                                         journal, company)
            if vals is None:
                stats['left_as_entry'] += 1
                problems.append(f'{key}: {why}')
                continue
            batch.append(vals)
            batch_old |= entry
            if len(batch) >= 200:
                flush()
        flush()
        self._apply_cost_centres_if_ready(date_from, date_to, company)
        reasons = defaultdict(int)
        for p in problems:
            reasons[p.split(': ', 1)[1].split(' (')[0]] += 1
        res = {**stats, 'reasons': dict(reasons), 'sample': problems[:20]}
        _logger.info('BAS bills %s..%s: %s', date_from, date_to, res)
        return res

    @api.model
    def _expected_vat(self, lines, company):
        """15% VAT as Odoo will compute it on this bill (the company's rounding)."""
        cur = company.currency_id
        if company.tax_calculation_rounding_method == 'round_globally':
            return cur.round(sum(a for _acc, a, _ref in lines) * 0.15)
        return sum(cur.round(a * 0.15) for _acc, a, _ref in lines)

    @api.model
    def _build_bill(self, bas_key, rows, accounts, partners, t15, t0, journal, company):
        """(vals, None) for an ``in_invoice``, or (None, reason)."""
        credits = [r for r in rows if r['TCODE']]
        debits = [r for r in rows if r['FCODE']]
        suppliers = {r['TCODE'] for r in credits}
        if len(suppliers) != 1 or not next(iter(suppliers)).startswith(_PAYABLE_PREFIX):
            return None, 'credits more than one supplier, or something besides a supplier'
        code = next(iter(suppliers))
        partner = partners.get(code)
        payable = accounts.get(code)
        if not partner or not payable:
            return None, f'no vendor for {code}'
        lines, vat, net = [], 0.0, 0.0
        for r in debits:
            acc = accounts.get(r['FCODE'])
            amount = float(r['AMOUNT'] or 0.0)
            if not acc:
                return None, f"unknown account {r['FCODE']}"
            if acc.account_type in _NOT_A_BILL_LINE:
                return None, f'debits {acc.account_type} (a payment or transfer, not a bill)'
            if r['FCODE'] == _VAT_INPUT_CODE:
                vat += amount
                continue
            if amount:
                lines.append((acc, round(amount, 2), (r['REF'] or '').strip()))
                net += amount
        if not lines:
            return None, 'no expense lines'
        total = sum(float(r['AMOUNT'] or 0.0) for r in credits)
        if abs(round(net + vat, 2) - round(total, 2)) > 0.01:
            return None, 'unbalanced at source'
        vat = round(vat, 2)
        cur = company.currency_id
        if not vat:
            tax = t0
        elif t15 and cur.compare_amounts(self._expected_vat(lines, company), vat) == 0:
            tax = t15
        else:
            return None, 'VAT is not 15% of the lines'
        if not tax:
            return None, 'no purchase tax configured'
        p = partner.with_company(company)
        if p.property_account_payable_id != payable:
            p.property_account_payable_id = payable.id
        day = rows[0]['FDATE'].date() if rows[0]['FDATE'] else False
        ftype, ftype2, code2, number1 = bas_key.split('/')
        return {
            'move_type': 'in_invoice',
            'partner_id': partner.id,
            'journal_id': journal.id,
            'company_id': company.id,
            'invoice_date': day,
            'date': day,
            'ref': f'BAS {ftype}/{ftype2} br{code2} #{number1}',
            'x_bas_key': bas_key,
            'x_bas_ftype': f'{ftype}/{ftype2}',
            'x_bas_branch': code2,
            'invoice_line_ids': [(0, 0, {
                'name': ref or acc.with_context(lang='ar_001').name or '/',
                'quantity': 1.0, 'price_unit': amount, 'account_id': acc.id,
                'tax_ids': [(6, 0, tax.ids)],
            }) for acc, amount, ref in lines],
        }, None

    # ------------------------------------------------------------------
    # Receipts / payments -> invoices / bills
    # ------------------------------------------------------------------
    @api.model
    def _named_invoice_numbers(self, date_to):
        """{(receipt voucher key, client account): {invoice numbers in its text}}."""
        conn = self._bas_connect()
        cur = conn.cursor(as_dict=True)
        cur.execute("""
            SELECT FTYPE, FTYPE2, RTRIM(CODE2) CODE2, NUMBER1, RTRIM(TCODE) T, REF
            FROM vou10
            WHERE FTYPE IN ('018', '015') AND LEFT(RTRIM(TCODE), 4) = '1203'
              AND FDATE < DATEADD(day, 1, %s) AND ISNULL(REF, '') <> ''
        """, (date_to,))
        out = defaultdict(set)
        for r in cur.fetchall():
            nums = set(_INVOICE_NO_RE.findall(r['REF'] or ''))
            if nums:
                out[(f"{r['FTYPE']}/{r['FTYPE2']}/{r['CODE2']}/{r['NUMBER1']:.0f}", r['T'])] |= nums
        conn.close()
        return out

    @api.model
    def action_link_partner_ledgers(self, date_to=None, account_types=(
            'asset_receivable', 'liability_payable'), fifo=True, named=True,
            account_codes=None, company=None):
        """Tag every client/vendor line with its partner and match it.

        Up to ``date_to`` only, so a later period is left untouched. Per BAS
        leaf account (one leaf = one partner):

        1. named: a receipt whose BAS text names one of this client's
           invoices pays that invoice (clients only; supplier texts do not
           carry our document numbers);
        2. exact: a payment equal to one open document dated on or before it;
        3. oldest first (``fifo``): everything still open is matched in date
           order, so the oldest invoice is settled first and partial
           payments stay partial.
        """
        company = company or self.env.company
        Account = self.env['account.account'].with_company(company)
        domain = [*Account._check_company_domain(company),
                  ('account_type', 'in', list(account_types)),
                  ('x_bas_code', '!=', False), ('reconcile', '=', True)]
        if account_codes:
            domain.append(('code', 'in', list(account_codes)))
        accounts = Account.search(domain)
        partners = {p.x_bas_code: p for p in self.env['res.partner'].search(
            [('x_bas_code', 'in', accounts.mapped('x_bas_code'))])}
        names = self._named_invoice_numbers(date_to or fields.Date.today()) \
            if named and 'asset_receivable' in account_types else {}

        exception = self.env['account.lock_exception'].create({
            'company_id': company.id, 'user_id': self.env.user.id,
            'lock_date_field': 'fiscalyear_lock_date', 'lock_date': False,
            'end_datetime': fields.Datetime.now() + timedelta(hours=6),
            'reason': _('BAS partner ledgers: tag the partner on its lines'),
        }) if company.fiscalyear_lock_date else None

        AML = self.env['account.move.line']
        cur = company.currency_id
        stats = defaultdict(int)
        try:
            for acc in accounts:
                partner = partners.get(acc.x_bas_code)
                if not partner:
                    stats['no_partner'] += 1
                    continue
                ldom = [('account_id', '=', acc.id), ('parent_state', '=', 'posted')]
                if date_to:
                    ldom.append(('date', '<=', date_to))
                lines = AML.search(ldom)
                untagged = lines.filtered(lambda l: not l.partner_id)
                if untagged:
                    untagged.write({'partner_id': partner.id})
                    stats['tagged'] += len(untagged)
                sign = 1 if acc.account_type == 'asset_receivable' else -1

                def open_docs():
                    return lines.filtered(lambda l: not l.reconciled and l.balance * sign > 0
                                          ).sorted(lambda l: (l.date, l.id))

                def open_pays():
                    return lines.filtered(lambda l: not l.reconciled and l.balance * sign < 0
                                          ).sorted(lambda l: (l.date, l.id))

                if names:
                    for pay in open_pays():
                        nums = names.get((pay.move_id.x_bas_key, acc.x_bas_code))
                        if not nums:
                            continue
                        docs = open_docs().filtered(
                            lambda l: (l.move_id.x_bas_key or '').split('/')[0] in ('001', '002')
                            and (l.move_id.x_bas_key or '').rsplit('/', 1)[-1] in nums)
                        if docs:
                            (docs | pay).reconcile()
                            stats['named'] += 1
                for pay in open_pays():
                    for doc in open_docs():
                        if (doc.date <= pay.date and cur.compare_amounts(
                                doc.amount_residual, -pay.amount_residual) == 0):
                            (doc | pay).reconcile()
                            stats['exact'] += 1
                            break
                if fifo:
                    docs, pays = open_docs(), open_pays()
                    if docs and pays:
                        # Debits then credits, each in date order: Odoo pairs
                        # them in that order (_prepare_reconciliation_amls).
                        (docs | pays).reconcile()
                        stats['oldest_first_accounts'] += 1
                stats['accounts'] += 1
                if stats['accounts'] % 25 == 0:
                    self.env.cr.commit()
        finally:
            if exception:
                exception.action_revoke()
        _logger.info('BAS partner ledgers: %s', dict(stats))
        return dict(stats)

    # ------------------------------------------------------------------
    # Monthly invoice lines rebuilt from its delivery notes
    # ------------------------------------------------------------------
    @api.model
    def action_rebuild_invoice_lines(self, date_from, date_to, company=None, bas_keys=None):
        """Give each BAS monthly invoice the lines Odoo itself would have made.

        The converter only saw the GL voucher, so an invoice had one line per
        revenue account ("مبيعات فرع العثمانية /تريلات"). Its delivery notes
        are now linked, so each becomes its own line (product, quantity and
        price from STR10, revenue account from the note's own 600 voucher)
        under a section per product: the shape of
        ``stock.picking._water_create_invoice``.

        Only when the notes add up to what BAS invoiced, per revenue account,
        to the halala: the invoice total, VAT and receivable never move. The
        payments matched to the invoice are matched again after the rebuild.
        """
        company = company or self.env.company
        Move = self.env['account.move'].with_context(tracking_disable=True)
        domain = [('move_type', '=', 'out_invoice'), ('state', '=', 'posted'),
                  ('company_id', '=', company.id), ('x_bas_key', '=like', '002/%'),
                  ('date', '>=', date_from), ('date', '<=', date_to),
                  ('x_bas_delivery_ids', '!=', False)]
        if bas_keys:
            domain.append(('x_bas_key', 'in', list(bas_keys)))
        invoices = Move.search(domain)
        products = {p.x_bas_code: p for p in self.env['product.product'].search(
            [('x_bas_code', '!=', False)])}
        accounts = {a.x_bas_code: a for a in self.env['account.account'].search(
            [('x_bas_code', '!=', False)])}
        stats, problems = defaultdict(int), []
        water = 'x_water_summary' in Move._fields

        for start in range(0, len(invoices), 50):
            chunk = invoices[start:start + 50]
            notes, revenue = self._invoice_note_lines(chunk.mapped('x_bas_key'))
            for inv in chunk:
                vals, why = self._rebuilt_lines(inv, notes.get(inv.x_bas_key, []),
                                                revenue, products, accounts, water)
                if vals is None:
                    stats['left_as_is'] += 1
                    problems.append(f'{inv.x_bas_key}: {why}')
                    continue
                try:
                    with self.env.cr.savepoint():
                        self._replace_invoice_lines(inv, vals)
                except UserError as e:
                    stats['left_as_is'] += 1
                    problems.append(f'{inv.x_bas_key}: total would move ({e})')
                    continue
                stats['rebuilt'] += 1
                stats['lines'] += sum(1 for c in vals['invoice_line_ids']
                                      if isinstance(c[2], dict)
                                      and c[2].get('display_type') != 'line_section')
            self.env.cr.commit()
        self._apply_cost_centres_if_ready(date_from, date_to, company)
        reasons = defaultdict(int)
        for p in problems:
            reasons[p.split(': ', 1)[1].split(' (')[0]] += 1
        res = {**stats, 'reasons': dict(reasons), 'sample': problems[:20]}
        _logger.info('BAS invoice lines rebuilt %s..%s: %s', date_from, date_to, res)
        return res

    @api.model
    def _invoice_note_lines(self, invoice_keys):
        """({invoice key: [STR10 rows of its notes]}, {note key: revenue code})."""
        wanted = defaultdict(set)              # (ftype2, code2) -> numbers
        for key in invoice_keys:
            _f, f2, c2, n = key.split('/')
            wanted[(int(f2), c2)].add(int(n))
        conn = self._bas_connect()
        cur = conn.cursor(as_dict=True)
        rows, revenue = defaultdict(list), {}
        for (f2, c2), nums in wanted.items():
            ph = ','.join(['%s'] * len(nums))
            args = (f2, c2, *sorted(nums))
            cur.execute(f"""
                SELECT FTYPE2, RTRIM(CODE2) CODE2, CAST(NUMBER1 AS BIGINT) N, SERIAL,
                       RTRIM(ICODE) ICODE, IDSCR, QUAN, AMOUNT, FDATE,
                       CAST(LINKED_NUMBER1 AS BIGINT) LN
                FROM STR10
                WHERE FTYPE = '600' AND LINKED_FTYPE = '002' AND LINKED_FTYPE2 = %s
                  AND RTRIM(LINKED_CODE2) = %s AND CAST(LINKED_NUMBER1 AS BIGINT) IN ({ph})
            """, args)
            for r in cur.fetchall():
                rows[f"002/{f2}/{c2}/{r['LN']}"].append(r)
            cur.execute(f"""
                SELECT v.FTYPE2, RTRIM(v.CODE2) CODE2, CAST(v.NUMBER1 AS BIGINT) N,
                       RTRIM(v.TCODE) TCODE
                FROM vou10 v
                WHERE v.FTYPE = '600' AND RTRIM(ISNULL(v.TCODE, '')) NOT IN ('', %s)
                  AND EXISTS (SELECT 1 FROM STR10 s
                              WHERE s.FTYPE = '600' AND s.FTYPE2 = v.FTYPE2
                                AND s.CODE2 = v.CODE2 AND s.NUMBER1 = v.NUMBER1
                                AND s.LINKED_FTYPE = '002' AND s.LINKED_FTYPE2 = %s
                                AND RTRIM(s.LINKED_CODE2) = %s
                                AND CAST(s.LINKED_NUMBER1 AS BIGINT) IN ({ph}))
            """, ('2119010001', *args))
            for r in cur.fetchall():
                revenue.setdefault(f"600/{r['FTYPE2']}/{r['CODE2']}/{r['N']}", r['TCODE'])
        conn.close()
        return rows, revenue

    @api.model
    def _rebuilt_lines(self, inv, rows, revenue, products, accounts, water):
        """(vals for ``inv.write``, None) or (None, reason)."""
        old = inv.invoice_line_ids.filtered(lambda l: l.display_type == 'product')
        taxes = old.tax_ids
        if len(taxes) > 1 or any(l.tax_ids != taxes for l in old):
            return None, 'invoice mixes taxes'
        if not rows:
            return None, 'no delivery lines in BAS'
        pickings = {p.x_bas_key: p for p in inv.x_bas_delivery_ids}
        cur = inv.currency_id
        built, by_acc = [], defaultdict(float)
        for r in rows:
            note = f"600/{r['FTYPE2']}/{r['CODE2']}/{r['N']}"
            amount = cur.round(float(r['AMOUNT'] or 0.0))
            if not amount:
                continue
            product = products.get(r['ICODE'])
            acc = accounts.get(revenue.get(note))
            if not product:
                return None, f"unknown item {r['ICODE']}"
            if not acc:
                return None, f'no revenue account on note {note}'
            qty = float(r['QUAN'] or 0.0) or 1.0
            picking = pickings.get(note)
            day = r['FDATE'].date() if r['FDATE'] else False
            built.append({'product': product, 'qty': qty, 'amount': amount, 'acc': acc,
                          'name': '%s · %s' % (picking.name if picking else note, day or ''),
                          'day': day or fields.Date.today(), 'note': note})
            by_acc[acc] += amount
        old_acc = defaultdict(float)
        for l in old:
            old_acc[l.account_id] += l.price_subtotal
        diff = {a.x_bas_code: round(by_acc.get(a, 0.0) - old_acc.get(a, 0.0), 2)
                for a in set(by_acc) | set(old_acc)}
        diff = {k: v for k, v in diff.items() if abs(v) > 0.005}
        if diff:
            return None, f'notes do not add up to the invoice ({diff})'

        groups = defaultdict(list)
        for b in built:
            groups[(b['product'], round(b['amount'] / b['qty'], 4))].append(b)
        commands, seq = [Command.delete(l.id) for l in old], 0
        for (product, price), group in sorted(groups.items(),
                                              key=lambda kv: (kv[0][0].display_name, kv[0][1])):
            group.sort(key=lambda b: (b['day'], b['name']))
            seq += 1
            section = {
                'display_type': 'line_section', 'sequence': seq,
                'name': _('%(product)s: %(qty)s %(uom)s at %(price)s (%(count)s notes)',
                          product=product.display_name,
                          qty=('%.4f' % sum(b['qty'] for b in group)).rstrip('0').rstrip('.'),
                          uom=product.uom_id.name, price=f'{price:g}', count=len(group)),
            }
            if water:
                section['x_water_section'] = True
            commands.append(Command.create(section))
            for b in group:
                seq += 1
                commands.append(Command.create({
                    'sequence': seq, 'product_id': product.id, 'name': b['name'],
                    'quantity': b['qty'], 'price_unit': b['amount'] / b['qty'],
                    'account_id': b['acc'].id, 'tax_ids': [Command.set(taxes.ids)],
                }))
        days = [b['day'] for b in built]
        vals = {'invoice_line_ids': commands,
                'invoice_origin': _('%(count)s delivery notes, %(start)s to %(end)s',
                                    count=len({b['note'] for b in built}),
                                    start=min(days), end=max(days))}
        if water:
            vals['x_water_summary'] = True
        return vals, None

    @api.model
    def _replace_invoice_lines(self, inv, vals):
        """Rewrite a posted invoice's lines without moving its total or payments."""
        rec = inv.line_ids.filtered(lambda l: l.account_id.account_type == 'asset_receivable')
        partners = (rec.matched_debit_ids.debit_move_id | rec.matched_credit_ids.credit_move_id) - rec
        total, tax = inv.amount_total, inv.amount_tax
        inv.button_draft()
        inv.write(vals)
        if inv.currency_id.compare_amounts(inv.amount_total, total) or \
                inv.currency_id.compare_amounts(inv.amount_tax, tax):
            raise UserError(_('%(inv)s: rebuilding the lines moved the total from '
                              '%(old)s to %(new)s.', inv=inv.name, old=total, new=inv.amount_total))
        inv._post(soft=False)
        rec = inv.line_ids.filtered(lambda l: l.account_id.account_type == 'asset_receivable')
        others = partners.filtered(lambda l: not l.reconciled)
        if others:
            (rec | others).reconcile()
