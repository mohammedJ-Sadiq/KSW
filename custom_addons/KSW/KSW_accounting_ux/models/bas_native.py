"""BAS's own information on the imported documents, one optional column away.

Every imported document keeps what BAS knew about it: document type (as BAS
names it), BAS number (branch / number), reference and remarks, and who
entered it, on the document and on each journal item. Read from vou10 by
``action_fill_bas_document_info``; nothing posted is changed, these fields only
add information, so nobody has to go back to BAS to read it.
"""
import logging
from collections import defaultdict

from odoo import api, fields, models
from odoo.fields import Domain

_logger = logging.getLogger(__name__)

#: BAS document type → how BAS names it (shown as "BAS Document Type").
BAS_TYPES = {
    '001': 'فاتورة مبيعات',
    '002': 'فاتورة مبيعات آجل',
    '006': 'فاتورة مشتريات',
    '015': 'سند قبض بنكي',
    '018': 'سند / قيد يومية',
    '101': 'إذن صرف مخزون',
    '600': 'إذن تسليم',
    'OPENING': 'أرصدة افتتاحية',
}


class AccountMove(models.Model):
    _inherit = 'account.move'

    x_bas_doc_type = fields.Char(string='BAS Document Type', compute='_compute_bas_doc', store=True)
    # Not stored: the branch name is translated, and a stored Char holds one language.
    x_bas_number = fields.Char(string='BAS Document No.', compute='_compute_bas_number',
                               search='_search_bas_number',
                               help='Branch / number of the document in BAS.')
    x_bas_reference = fields.Char(string='BAS Reference', readonly=True, copy=False,
                                  help="The document's reference text in BAS (REF).")
    x_bas_remarks = fields.Char(string='BAS Remarks', readonly=True, copy=False,
                                help="The document's remarks in BAS (REMARK).")
    x_bas_entered_by = fields.Char(string='BAS Entered By', readonly=True, copy=False,
                                   help='BAS user number who entered the document.')

    @api.depends('x_bas_key', 'x_bas_ftype')
    def _compute_bas_doc(self):
        for move in self:
            ftype = (move.x_bas_key or '').split('/')[0]
            move.x_bas_doc_type = BAS_TYPES.get(ftype, ftype) if move.x_bas_key else False

    @api.depends('x_bas_key', 'x_bas_branch_id.name')
    @api.depends_context('lang')
    def _compute_bas_number(self):
        """Branch name / number -- the branch as people call it, never CODE2."""
        for move in self:
            parts = (move.x_bas_key or '').split('/')
            if len(parts) == 4:
                branch = move.sudo().x_bas_branch_id.name or parts[2]
                move.x_bas_number = f'{branch} / {parts[3]}'
            else:
                move.x_bas_number = move.x_bas_key or False

    def _search_bas_number(self, operator, value):
        if operator not in ('ilike', '=ilike', '=', 'like') or not isinstance(value, str):
            return NotImplemented
        value = value.strip()
        # "155/15401" as BAS prints it is the tail of x_bas_key; the number
        # alone or the branch name are matched too.
        domain = Domain('x_bas_key', '=like', f'%/{value}') if value.count('/') == 1 \
            else Domain('x_bas_key', 'ilike', value)
        domain |= Domain('x_bas_branch_id.name', 'ilike', value)
        if ' / ' in value:
            # The displayed form, "<branch> / <number>".
            name, number = value.rsplit(' / ', 1)
            domain |= Domain('x_bas_branch_id.name', 'ilike', name.strip()) \
                & Domain('x_bas_key', '=like', f'%/{number.strip()}')
        return domain


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    x_bas_line_ref = fields.Char(string='BAS Line Reference', readonly=True, copy=False,
                                 help="This line's reference text in BAS (REF).")
    x_bas_line_remark = fields.Char(string='BAS Line Remark', readonly=True, copy=False)
    x_bas_doc_type = fields.Char(related='move_id.x_bas_doc_type', string='BAS Document Type')
    x_bas_number = fields.Char(related='move_id.x_bas_number', string='BAS Document No.')


class BasGlImport(models.Model):
    _inherit = 'ksw.bas.gl.import'

    # ------------------------------------------------------------------
    # BAS information on the documents
    # ------------------------------------------------------------------
    @api.model
    def action_fill_bas_document_info(self, date_from=None, date_to=None, company=None, commit=True):
        """Reference, remarks and user from BAS, on each document and journal item."""
        company = company or self.env.company
        Move = self.env['account.move'].sudo()
        domain = [('company_id', '=', company.id), ('x_bas_key', '=like', '%/%/%/%')]
        if date_from:
            domain.append(('date', '>=', date_from))
        if date_to:
            domain.append(('date', '<=', date_to))
        moves = Move.search(domain, order='date, id')
        stats = defaultdict(int)
        conn = self._bas_connect()
        try:
            cur = conn.cursor(as_dict=True)
            for i in range(0, len(moves), 400):
                chunk = moves[i:i + 400]
                groups = defaultdict(list)
                for m in chunk:
                    f, f2, c2, n = m.x_bas_key.split('/')
                    groups[(f, int(f2), c2)].append(int(n))
                rows = defaultdict(list)
                for (f, f2, c2), numbers in groups.items():
                    cur.execute(
                        "SELECT CAST(NUMBER1 AS bigint) N, SERIAL, RTRIM(ISNULL(FCODE,'')) FCODE, "
                        "RTRIM(ISNULL(TCODE,'')) TCODE, AMOUNT, BAMOUNT, RTRIM(ISNULL(REF,'')) REF, "
                        "RTRIM(ISNULL(REMARK,'')) REMARK, RTRIM(ISNULL(USER_NO,'')) USER_NO "
                        "FROM vou10 WHERE FTYPE = %s AND FTYPE2 = %s AND RTRIM(CODE2) = %s "
                        f"AND CAST(NUMBER1 AS bigint) IN ({','.join(['%s'] * len(numbers))}) ORDER BY SERIAL",
                        (f, f2, c2, *numbers))
                    for r in cur.fetchall():
                        rows[f'{f}/{f2}/{c2}/{r["N"]}'].append(r)
                for m in chunk:
                    bas = rows.get(m.x_bas_key, [])
                    if not bas:
                        stats['not in BAS'] += 1
                        continue
                    refs = list(dict.fromkeys(r['REF'] for r in bas if r['REF']))
                    remarks = list(dict.fromkeys(r['REMARK'] for r in bas if r['REMARK']))
                    users = list(dict.fromkeys(r['USER_NO'] for r in bas if r['USER_NO']))
                    m.write({'x_bas_reference': ' | '.join(refs)[:500] or False,
                             'x_bas_remarks': ' | '.join(remarks)[:500] or False,
                             'x_bas_entered_by': ', '.join(users) or False})
                    # Lines: the BAS row with the same account, side and amount.
                    pool = defaultdict(list)
                    for r in bas:
                        code, side = (r['FCODE'], 'D') if r['FCODE'] else (r['TCODE'], 'C')
                        amount = round(abs(float(r['AMOUNT'] or 0.0) or float(r['BAMOUNT'] or 0.0)), 2)
                        pool[(code, side)].append((amount, r))
                    for line in m.line_ids:
                        key = (line.account_id.x_bas_code, 'D' if line.debit else 'C')
                        cands = pool.get(key)
                        if not cands:
                            continue
                        amt = round(line.debit or line.credit, 2)
                        hit = next((c for c in cands if c[0] == amt), None)
                        if hit:
                            cands.remove(hit)
                        else:
                            texts = {c[1]['REF'] for c in cands}
                            hit = cands[0] if len(texts) == 1 else None
                        if hit and (hit[1]['REF'] or hit[1]['REMARK']):
                            line.write({'x_bas_line_ref': hit[1]['REF'] or False,
                                        'x_bas_line_remark': hit[1]['REMARK'] or False})
                            stats['lines'] += 1
                    stats['documents'] += 1
                if commit:
                    self.env.cr.commit()
        finally:
            conn.close()
        _logger.info('BAS document info: %s', dict(stats))
        return dict(stats)


class AccountMoveDocType(models.Model):
    _inherit = 'account.move'

    # One "what kind of document is this" for every entry, made in BAS or in
    # Odoo, so a report needs one filter whatever journal the entry sits in.
    x_doc_category = fields.Selection([
        ('customer_invoice', 'Customer Invoice'),
        ('cash_sale', 'Cash Sale'),
        ('credit_note', 'Customer Credit Note'),
        ('vendor_bill', 'Vendor Bill'),
        ('vendor_refund', 'Vendor Refund'),
        ('customer_payment', 'Customer Payment'),
        ('vendor_payment', 'Vendor Payment'),
        ('bank_receipt', 'Bank Receipt'),
        ('stock', 'Stock Movement'),
        ('depreciation', 'Depreciation'),
        ('opening', 'Opening Balance'),
        ('journal_voucher', 'Journal Voucher'),
    ], string='Document Type', compute='_compute_doc_category', store=True, index=True)

    @api.depends('move_type', 'journal_id', 'journal_id.code', 'journal_id.type', 'x_bas_key',
                 'origin_payment_id', 'origin_payment_id.payment_type', 'statement_line_id')
    def _compute_doc_category(self):
        by_ftype = {'001': 'cash_sale', '002': 'customer_invoice', '600': 'customer_invoice',
                    '006': 'vendor_bill', '015': 'bank_receipt', '101': 'stock', 'OPENING': 'opening'}
        by_type = {'out_invoice': 'customer_invoice', 'out_receipt': 'cash_sale',
                   'out_refund': 'credit_note', 'in_invoice': 'vendor_bill',
                   'in_receipt': 'vendor_bill', 'in_refund': 'vendor_refund'}
        for move in self:
            ftype = (move.x_bas_key or '').split('/')[0]
            code = move.journal_id.code
            if move.move_type in by_type:
                cat = by_type[move.move_type]
                if cat == 'customer_invoice' and (ftype == '001' or code in ('CSAL', 'BCSAL')):
                    cat = 'cash_sale'
            elif move.origin_payment_id:
                cat = 'customer_payment' if move.origin_payment_id.payment_type == 'inbound' else 'vendor_payment'
            elif ftype in by_ftype:
                cat = by_ftype[ftype]
            elif move.statement_line_id:
                cat = 'bank_receipt'
            elif code in ('STJ', 'BASRE'):
                cat = 'stock'
            elif code in ('DEPR', 'BASDP') or 'asset_id' in move._fields and move.asset_id:
                cat = 'depreciation'
            elif code in ('OPEN', 'BASOP'):
                cat = 'opening'
            else:
                cat = 'journal_voucher'
            move.x_doc_category = cat

    # Cash or credit, for the customer invoice list. A cash sale is a BAS 001
    # or anything in the Cash Sales journal; a credit note follows its journal.
    x_sale_terms = fields.Selection([('cash', 'Cash'), ('credit', 'Credit')], string='Cash / Credit',
                                    compute='_compute_sale_terms', store=True, index=True)

    @api.depends('move_type', 'x_doc_category', 'journal_id.code')
    def _compute_sale_terms(self):
        for move in self:
            if not move.is_sale_document(include_receipts=True):
                move.x_sale_terms = False
            elif move.x_doc_category == 'cash_sale' or move.journal_id.code in ('CSAL', 'BCSAL'):
                move.x_sale_terms = 'cash'
            else:
                move.x_sale_terms = 'credit'


class AccountMoveLineDocType(models.Model):
    _inherit = 'account.move.line'

    x_doc_category = fields.Selection(related='move_id.x_doc_category', store=True, index=True,
                                      string='Document Type')


#: The BAS journals become the journals Odoo works in (authorised by the user
#: 2026-10-23): (old code, new code, English, Arabic, type). Only journal
#: settings change, through the journal's own write: no posted document is
#: touched, old documents keep their numbers, new ones follow the new code.
STANDARD_JOURNALS = [
    ('BCINV', 'INV', 'Customer Invoices', 'فواتير العملاء', 'sale'),
    ('BCSAL', 'CSAL', 'Cash Sales', 'المبيعات النقدية', 'sale'),
    ('BCBIL', 'BILL', 'Vendor Bills', 'فواتير الموردين', 'purchase'),
    ('BASCS', 'MISC', 'Miscellaneous Operations', 'العمليات المتنوعة', 'general'),
    ('BASBN', 'BRCPT', 'Bank Receipts', 'المقبوضات البنكية', 'general'),
    # Holds the BAS 101 stock issues (it was called "Sales Returns").
    ('BASRE', 'STJ', 'Inventory Valuation', 'تقييم المخزون', 'general'),
    ('BASOP', 'OPEN', 'Opening Balances', 'الأرصدة الافتتاحية', 'general'),
    ('BASDP', 'DEPR', 'Depreciation', 'الإهلاك', 'general'),
]
#: Where each BAS document type is imported once the journals are standard.
IMPORT_JOURNAL_BY_FTYPE = {'001': 'CSAL', '002': 'INV', '600': 'INV', '101': 'STJ', '018': 'MISC',
                           '015': 'BRCPT', '006': 'MISC', 'OPEN': 'OPEN', 'DEPR': 'DEPR'}


class BasGlImportJournals(models.Model):
    _inherit = 'ksw.bas.gl.import'

    def _standard_journals_done(self, company):
        """Routing only switches once the BAS journals carry the standard codes."""
        return bool(self.env['account.journal'].search_count(
            [('company_id', '=', company.id), ('code', '=', 'CSAL')], limit=1))

    def _journal(self, ftype, company):
        code = IMPORT_JOURNAL_BY_FTYPE.get(ftype)
        if code and self._standard_journals_done(company):
            journal = self.env['account.journal'].search(
                [('company_id', '=', company.id), ('code', '=', code)], limit=1)
            if journal:
                return journal
        return super()._journal(ftype, company)

    def _bill_journal(self, company):
        if self._standard_journals_done(company):
            journal = self.env['account.journal'].search(
                [('company_id', '=', company.id), ('code', '=', 'BILL'), ('type', '=', 'purchase')], limit=1)
            if journal:
                return journal
        return super()._bill_journal(company)

    @api.model
    def action_standardise_bas_journals(self, company=None):
        """Make the BAS journals the standard ones; retire Odoo's unused duplicates."""
        company = company or self.env.company
        Journal = self.env['account.journal'].sudo().with_context(active_test=False)
        Move = self.env['account.move'].sudo()
        by_code = {j.code: j for j in Journal.search([('company_id', '=', company.id)])}
        log = []
        for old, new, name_en, name_ar, jtype in STANDARD_JOURNALS:
            journal = by_code.get(old)
            if not journal:
                continue
            clash = by_code.get(new)
            if clash and clash != journal:
                count = Move.search_count([('journal_id', '=', clash.id)])
                retired = ('X' + new)[:5]
                clash.write({'code': retired, 'active': False,
                             'name': f'{clash.name} (replaced by {name_en})'})
                # The code must be free in the database before it is reused.
                clash.flush_recordset()
                by_code[retired] = clash
                log.append(f'retired {new} (id {clash.id}, {count} documents) as {retired}')
            vals = {'code': new, 'name': name_en, 'active': True}
            if journal.type != jtype:
                vals['type'] = jtype
            journal.write(vals)
            journal.flush_recordset()
            journal.update_field_translations('name', {'ar_001': name_ar})
            by_code[new] = journal
            log.append(f'{old} → {new} «{name_en}»')
        for code in ('BASIN', 'BASPO', 'BASMS'):
            j = by_code.get(code)
            if j and j.active and not Move.search_count([('journal_id', '=', j.id)]):
                j.active = False
                log.append(f'archived empty {code}')
        stj = by_code.get('STJ')
        if stj and company.account_stock_journal_id != stj:
            company.account_stock_journal_id = stj
            log.append('company stock journal → STJ')
        _logger.info('BAS journals standardised: %s', log)
        return log


class AccountMoveStandardSequence(models.Model):
    _inherit = 'account.move'

    def _get_last_sequence_domain(self, relaxed=False):
        """In a renamed BAS journal, continue only numbers with its current code.

        Odoo continues the last number it finds in the journal, whatever its
        format, so a new invoice in Customer Invoices would read BCINV/… after
        the rename. Restricting the search to the journal's own prefix makes the
        first new document start INV/… (or RINV/…, BILL/…) while the imported
        ones keep their numbers.
        """
        where_string, param = super()._get_last_sequence_domain(relaxed=relaxed)
        code = self.journal_id.code
        if code in IMPORT_JOURNAL_BY_FTYPE.values() or code == 'BILL':
            prefix = self._get_starting_sequence().split('/')[0]
            where_string += " AND sequence_prefix LIKE %(ksw_prefix)s"
            param['ksw_prefix'] = prefix + '/%'
        return where_string, param


class AccountMoveImportedAs(models.Model):
    _inherit = 'account.move'

    x_bas_old_name = fields.Char(
        string='Imported As', readonly=True, copy=False, index=True,
        help='The number this document had before it was renumbered in its standard journal.')


class BasGlImportRenumber(models.Model):
    _inherit = 'ksw.bas.gl.import'

    @api.model
    def action_renumber_bas_documents(self, company=None, journal_codes=None):
        """Renumber the imported documents natively with Odoo's own Resequence tool.

        Authorised by the user (2026-10-23). Per standard journal, per series
        (invoices / refunds), in date order, starting at the journal's first
        native number. Documents inside a locked period are left as they are.
        The old number is kept in ``x_bas_old_name`` and every text quoting it
        (payment reference, the document's own line labels, BAS payment memos,
        receipt origins) is updated; the purchase history follows by itself
        (a related field).
        """
        company = company or self.env.company
        Move = self.env['account.move'].sudo().with_context(tracking_disable=True)
        Journal = self.env['account.journal'].sudo()
        codes = journal_codes or ['INV', 'CSAL', 'BILL', 'MISC', 'BRCPT', 'STJ', 'DEPR', 'OPEN']
        lock = max(filter(None, [company.fiscalyear_lock_date, company.hard_lock_date]), default=False)
        refund_types = ('out_refund', 'in_refund')
        renamed, stats = {}, {}
        for journal in Journal.search([('company_id', '=', company.id), ('code', 'in', codes)]):
            moves = Move.search([('journal_id', '=', journal.id), ('state', '=', 'posted'),
                                 ('x_bas_key', '!=', False), ('origin_payment_id', '=', False)])
            if lock:
                moves = moves.filtered(lambda m: m.date > lock)
            groups = [moves.filtered(lambda m: m.move_type in refund_types),
                      moves.filtered(lambda m: m.move_type not in refund_types)] \
                if journal.refund_sequence else [moves]
            for group in groups:
                todo = group.filtered(lambda m: not (m.name or '').startswith(journal.code + '/')
                                      and not (m.name or '').startswith('R' + journal.code + '/'))
                if not todo:
                    continue
                # Never collide with numbers Odoo already gave in this series.
                native = Move.search_count([('journal_id', '=', journal.id), ('id', 'not in', todo.ids),
                                            ('move_type', 'in' if todo[0].move_type in refund_types else 'not in',
                                             refund_types),
                                            ('name', '=like', ('R' if todo[0].move_type in refund_types else '')
                                             + journal.code + '/%')])
                if native:
                    stats[journal.code] = f'skipped: {native} native numbers already in this series'
                    continue
                first = todo.sorted(lambda m: (m.date, m.id))[0]
                start = first._get_starting_sequence()
                fmt, vals = first._get_sequence_format_param(start)
                vals['seq'] = 1
                first_name = fmt.format(**vals)
                old = {m.id: m.name for m in todo}
                for m in todo:
                    if not m.x_bas_old_name:
                        m.x_bas_old_name = m.name
                wizard = self.env['account.resequence.wizard'].sudo().with_context(
                    active_model='account.move', active_ids=todo.ids).create(
                    {'ordering': 'date', 'first_name': first_name})
                wizard.resequence()
                for m in todo:
                    if m.name != old[m.id]:
                        renamed[m.id] = (old[m.id], m.name)
                stats[f'{journal.code}{" refunds" if group and group[0].move_type in refund_types else ""}'] = len(todo)
            if self.env.context.get('ksw_commit'):
                self.env.cr.commit()
        self._renumber_references(renamed)
        _logger.info('BAS documents renumbered: %s', stats)
        return {'renamed': len(renamed), 'per journal': stats}

    def _renumber_references(self, renamed):
        """Update every text that quotes a renamed document's old number."""
        if not renamed:
            return
        Move = self.env['account.move'].sudo().with_context(tracking_disable=True)
        moves = Move.browse(list(renamed))
        for move in moves:
            old, new = renamed[move.id]
            vals = {}
            if move.payment_reference and old in move.payment_reference:
                vals['payment_reference'] = move.payment_reference.replace(old, new)
            if move.ref and old in move.ref:
                vals['ref'] = move.ref.replace(old, new)
            if vals:
                move.write(vals)
            for line in move.line_ids.filtered(lambda l: l.name and old in l.name):
                line.name = line.name.replace(old, new)
        if 'x_bas_move_id' in self.env['account.payment']._fields:
            for pay in self.env['account.payment'].sudo().search([('x_bas_move_id', 'in', moves.ids)]):
                old, new = renamed[pay.x_bas_move_id.id]
                if pay.memo and old in pay.memo:
                    pay.with_context(ksw_bas_mirror_sync=True).memo = pay.memo.replace(old, new)
        by_key = {m.x_bas_key: renamed[m.id] for m in moves if m.x_bas_key}
        for picking in self.env['stock.picking'].sudo().search([('x_bas_key', 'in', list(by_key))]):
            old, new = by_key[picking.x_bas_key]
            if picking.x_bas_key.startswith('006/'):
                # A BAS receipt's source is its bill: quote the bill's own number.
                picking.origin = new
            elif picking.origin and old in picking.origin:
                picking.origin = picking.origin.replace(old, new)
        # Cross references elsewhere (a payment or entry quoting another document).
        old_to_new = dict(renamed.values())
        prefixes = sorted({old.split('/')[0] for old in old_to_new})
        AML = self.env['account.move.line'].sudo()
        domain = ['|'] * (len(prefixes) - 1) + [('name', 'like', p + '/') for p in prefixes]
        for line in AML.search(domain + [('move_id', 'not in', moves.ids)]):
            text = line.name
            for old, new in old_to_new.items():
                if old in text:
                    text = text.replace(old, new)
            if text != line.name:
                line.name = text
