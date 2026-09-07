import logging
import re
from collections import defaultdict

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# BAS account code -> Odoo account_type.
#
# Derived from BAS's own chart skeleton (COD10 levels 1-3), NOT from
# ``DACC_TYPE``: that column is documented as partly "Unknown" in
# BAS_DATABASE_REFERENCE.md and its 01/10 meanings were already found to be
# wrong once (corrected 2026-08-11).  The code hierarchy is self-describing
# and was read straight off the live database, so it is the safer key.
#
# Longest prefix wins -- order here is irrelevant, ``_account_type`` sorts.
# ---------------------------------------------------------------------------
_TYPE_BY_PREFIX = {
    # 1 -- الأصول (assets)
    '1201': 'asset_cash',          # حسابات النقدية
    '1202': 'asset_cash',          # حسابات البنوك
    '1203': 'asset_receivable',    # حسابات العملاء
    '1209': 'asset_current',       # ض.ق.م المدينة (input VAT)
    '1212': 'asset_prepayments',   # مدفوعات مقدمة
    '1219': 'asset_fixed',         # مشروعات تحت التنفيذ
    '12':   'asset_current',
    '13':   'asset_non_current',   # الاصول غير المتداولة
    '19':   'asset_fixed',         # الاصول الثابتة
    '1':    'asset_current',
    # 2 -- الخصوم (liabilities + equity)
    '2101': 'liability_payable',   # حسابات الموردين
    '2119': 'liability_current',   # ض.ق.م المبيعات (output VAT)
    '21':   'liability_current',
    '23':   'equity',              # حقوق الملكية
    # مجمع اهلاك -- BAS files accumulated depreciation under الخصوم, but it is
    # a contra-asset with a credit balance.  asset_fixed is the accounting
    # answer and nets correctly against 19xx on the balance sheet.
    '24':   'asset_fixed',
    '2':    'liability_current',
    # 3 -- المصروفات (expenses)
    '31':   'expense_direct_cost',  # المشتريات وتكلفة التشغيل
    '33':   'expense_depreciation', # اهلاك الاصول الثابتة
    '32':   'expense',
    '3':    'expense',
    # 4 -- الإيرادات (revenue)
    '41':   'income',              # المبيعات العامة
    '42':   'income_other',        # ايرادات اخرى
    '4':    'income',
}

# BAS FTYPE -> (journal code, journal name, journal type)
# NOTE: account.journal.code is capped at 5 characters, and Odoo TRUNCATES
# silently -- 'BASCSH' became 'BASCS'.  Two entries here whose first five
# characters collide will therefore share one journal, and if their types differ
# the second one dies with "Cannot create a sale document in a non sale journal".
# Every code below is already 5 characters and distinct.
_JOURNAL_BY_FTYPE = {
    '600': ('BASPOS', 'BAS POS Sales',       'sale'),
    '001': ('BCSAL', 'BAS Cash Sales',        'sale'),
    '002': ('BCINV', 'BAS Customer Invoices', 'sale'),
    '101': ('BASRET', 'BAS Sales Returns',   'sale'),
    '018': ('BASCSH', 'BAS Cash Receipts',   'general'),
    '015': ('BASBNK', 'BAS Bank Receipts',   'general'),
    '006': ('BASMSC', 'BAS Miscellaneous',   'general'),
    'OPEN': ('BASOP', 'BAS Opening Balances', 'general'),
}
_DEFAULT_JOURNAL = ('BASMSC', 'BAS Miscellaneous', 'general')

# FTYPEs whose vou10 rows carry the value in BAMOUNT with VAT split out into
# TAX_AMOUNT, instead of the usual AMOUNT.  Verified on the live database:
# every one of FTYPE 600's 562,480 vou10 lines has AMOUNT = 0, and
# BAMOUNT + TAX_AMOUNT reconciles to the STR10 line total to the halala.
_BAMOUNT_FTYPES = {'600'}

_VAT_OUTPUT_CODE = '2119010001'   # ضريبة القيمة المضافة -- VAT payable

# FTYPE 600 is a DELIVERY NOTE, cash or credit, and BAS posts nothing for it.
# Proven by three statements of account (2026-09-06): a till statement shows 231
# cash delivery notes on one day with the balance unmoved, and two customer
# statements show thousands of credit notes at debit 0 / credit 0.  vou10 carries
# a row for each anyway, which is why a raw table import produced 281,446
# fictitious journal entries.  The money is recognised by the documents that
# follow: 001 (بيع نقدا), 002 (بيع اجل لاذونات تسليم) and the receipts.
#
# These belong in Odoo as DELIVERY ORDERS, not accounting -- to be built with
# inventory.  They must never come back into the ledger.
_NON_POSTING_FTYPES = {'600'}

# ZATCA / base_vat rule for Saudi VAT registration numbers.
_SA_VAT_RE = re.compile(r'^3\d{13}3$')


class BasGlImport(models.Model):
    _name = 'ksw.bas.gl.import'
    _description = 'BAS General Ledger Import'
    _inherit = ['ksw.bas.connector']
    _order = 'id desc'

    name = fields.Char(default='BAS GL Import', readonly=True)
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda s: s.env.company)
    date_from = fields.Date(required=True)
    date_to = fields.Date(required=True)
    branch_code = fields.Char(
        help="Optional BAS branch (CODE2). Leave empty for all branches.")
    state = fields.Selection(
        [('draft', 'Draft'), ('done', 'Imported')], default='draft')

    voucher_count = fields.Integer(readonly=True)
    move_count = fields.Integer(readonly=True)
    skipped_count = fields.Integer(readonly=True)
    log = fields.Text(readonly=True)

    # ------------------------------------------------------------------
    # Chart of accounts
    # ------------------------------------------------------------------
    @api.model
    def _account_type(self, code):
        """Longest matching prefix wins."""
        for n in range(len(code), 0, -1):
            t = _TYPE_BY_PREFIX.get(code[:n])
            if t:
                return t
        return 'asset_current'

    @api.model
    def action_import_chart(self, company=None):
        """COD10 -> account.group (levels 1-4) + account.account (level 5)."""
        company = company or self.env.company
        conn = self._bas_connect()
        cur = conn.cursor(as_dict=True)
        cur.execute("""
            SELECT RTRIM(DCODE1) code, DNAME, DNAME2, DLEVEL
            FROM COD10 WHERE DCODE1 IS NOT NULL AND RTRIM(DCODE1) <> ''
            ORDER BY DLEVEL, DCODE1
        """)
        rows = cur.fetchall()
        conn.close()

        Group = self.env['account.group'].with_company(company)
        Account = self.env['account.account'].with_company(company)

        groups = {g.x_bas_code: g for g in Group.search(
            [('x_bas_code', '!=', False), ('company_id', '=', company.id)])}
        accounts = {a.x_bas_code: a for a in Account.search(
            [('x_bas_code', '!=', False)])}

        made_g = made_a = 0
        for r in rows:
            code = (r['code'] or '').strip()
            name = (r['DNAME'] or r['DNAME2'] or code).strip()
            level = int(r['DLEVEL'] or 5)
            if not code:
                continue
            if level <= 4:
                if code in groups:
                    continue
                parent = None
                for n in range(len(code) - 1, 0, -1):
                    parent = groups.get(code[:n])
                    if parent:
                        break
                groups[code] = Group.create({
                    'name': name, 'x_bas_code': code,
                    'code_prefix_start': code, 'code_prefix_end': code,
                    'parent_id': parent.id if parent else False,
                    'company_id': company.id,
                })
                made_g += 1
            else:
                if code in accounts:
                    continue
                accounts[code] = Account.create({
                    'code': code, 'name': name,
                    'x_bas_code': code, 'x_bas_name_ar': (r['DNAME'] or '').strip(),
                    'account_type': self._account_type(code),
                    'company_ids': [(4, company.id)],
                })
                made_a += 1
        _logger.info('BAS chart import: %s groups, %s accounts created', made_g, made_a)
        return {'groups': made_g, 'accounts': made_a}

    # ------------------------------------------------------------------
    # Journals
    # ------------------------------------------------------------------
    def _journal(self, ftype, company):
        cache = self.env.context.get('bas_journal_cache')
        if cache is not None and ftype in cache:
            return self.env['account.journal'].browse(cache[ftype])
        code, name, jtype = _JOURNAL_BY_FTYPE.get(ftype, _DEFAULT_JOURNAL)
        Journal = self.env['account.journal']
        j = Journal.search(
            [('code', '=', code), ('company_id', '=', company.id)], limit=1)
        if not j:
            j = Journal.create({
                'name': name, 'code': code, 'type': jtype,
                'company_id': company.id,
            })
        # A new SALE journal inherits the sa_zatca EDI format, and l10n_sa_edi
        # then refuses to post anything until that journal is onboarded with a
        # CSR/CSID.  Migrated history was ALREADY cleared through ZATCA by BAS --
        # re-submitting it would be wrong -- so detach the format from every
        # journal this module owns.  Done here rather than by hand: doing it once
        # manually missed the journals created later, and the failed post rolled
        # the journal creation back, which made it look like the fix had worked.
        zatca = self.env.ref('l10n_sa_edi.edi_sa_zatca', raise_if_not_found=False)
        if zatca and 'edi_format_ids' in j._fields and zatca in j.edi_format_ids:
            j.edi_format_ids = [(3, zatca.id)]
        if cache is not None:
            cache[ftype] = j.id
        return j

    # ------------------------------------------------------------------
    # Ledger
    # ------------------------------------------------------------------
    def action_import(self):
        self.ensure_one()
        company = self.company_id
        accounts = {
            a.x_bas_code: a.id
            for a in self.env['account.account'].search([('x_bas_code', '!=', False)])
        }
        if not accounts:
            raise UserError(_('Import the BAS chart of accounts first.'))
        vat_id = accounts.get(_VAT_OUTPUT_CODE)

        conn = self._bas_connect()
        cur = conn.cursor(as_dict=True)
        params = [self.date_from, self.date_to]
        branch_sql = ''
        if self.branch_code:
            branch_sql = ' AND RTRIM(CODE2) = %s'
            params.append(self.branch_code)
        cur.execute(f"""
            SELECT FTYPE, FTYPE2, RTRIM(CODE2) CODE2, NUMBER1, SERIAL,
                   RTRIM(FCODE) FCODE, RTRIM(TCODE) TCODE,
                   AMOUNT, BAMOUNT, TAX_AMOUNT, FDATE
            FROM vou10
            WHERE FDATE >= %s AND FDATE < DATEADD(day, 1, %s){branch_sql}
              AND FTYPE NOT IN ('600')
            ORDER BY FTYPE, FTYPE2, CODE2, NUMBER1, SERIAL
        """, tuple(params))
        rows = cur.fetchall()
        conn.close()

        vouchers = defaultdict(list)
        for r in rows:
            key = (r['FTYPE'], r['FTYPE2'], r['CODE2'], r['NUMBER1'])
            vouchers[key].append(r)

        existing = set(self.env['account.move'].search([
            ('x_bas_key', '!=', False), ('company_id', '=', company.id),
        ]).mapped('x_bas_key'))

        Move = self.env['account.move'].with_context(
            tracking_disable=True, check_move_validity=False).with_company(company)

        self = self.with_context(bas_journal_cache={})
        batch, made, skipped, problems = [], 0, 0, []
        for key, lines in vouchers.items():
            ftype, ftype2, code2, number1 = key
            bas_key = f'{ftype}/{ftype2}/{code2}/{number1:.0f}'
            if bas_key in existing:
                continue
            vals = self._voucher_to_move(
                bas_key, ftype, ftype2, code2, number1, lines,
                accounts, vat_id, company, problems)
            if vals is None:
                skipped += 1
                continue
            batch.append(vals)
            if len(batch) >= 200:
                made += self._flush_moves(Move, batch)
                batch = []
                # Checkpoint: a full-year run is hours long and must not lose
                # everything to one interruption.  x_bas_key makes it resumable.
                self.env.cr.commit()
        if batch:
            made += self._flush_moves(Move, batch)

        self.write({
            'state': 'done',
            'voucher_count': len(vouchers),
            'move_count': made,
            'skipped_count': skipped,
            'log': '\n'.join(problems[:200]) or 'No problems.',
        })
        return True

    def _voucher_to_move(self, bas_key, ftype, ftype2, code2, number1, lines,
                         accounts, vat_id, company, problems):
        """Build one account.move from a BAS voucher's vou10 lines.

        FCODE is the debit side, TCODE the credit side -- each vou10 row
        carries one or the other, never both.
        """
        use_b = ftype in _BAMOUNT_FTYPES
        raw, missing = [], set()

        # Accumulate at FULL precision first.  BAS stores half-halalas (3 dp,
        # e.g. 25927.785 = 549.9 + 21996.0 + 3381.885), so rounding each line
        # to 2 dp independently breaks a voucher that balances exactly at
        # source -- that cost 14 January vouchers, one of them 691,180 SAR.
        for r in lines:
            tax = float(r['TAX_AMOUNT'] or 0.0)
            val = float((r['BAMOUNT'] if use_b else r['AMOUNT']) or 0.0)
            if not val and not tax:
                continue
            fcode, tcode = (r['FCODE'] or '').strip(), (r['TCODE'] or '').strip()
            code = fcode or tcode
            if not code:
                # A line with neither FCODE nor TCODE is a hole in BAS itself.
                # Record it so the voucher fails loudly instead of posting a
                # one-sided entry.
                missing.add('<blank account on serial %s>' % r.get('SERIAL'))
                continue
            acc = accounts.get(code)
            if acc is None:
                missing.add(code)
                continue
            if fcode:
                raw.append([acc, val + (tax if use_b else 0.0), 0.0])
            else:
                raw.append([acc, 0.0, val])
                if use_b and tax:
                    raw.append([vat_id, 0.0, tax] if vat_id else [acc, 0.0, 0.0])

        if missing:
            problems.append(f'{bas_key}: unusable account(s) {sorted(missing)[:4]}')
            return None
        if not raw:
            # Every skip must be attributable.  These are vouchers whose lines
            # are all zero-value (BAS keeps them; there is nothing to post) --
            # harmless, but they must not vanish from the count unexplained.
            problems.append(f'{bas_key}: no postable lines (all amounts zero)')
            return None

        raw_d = sum(x[1] for x in raw)
        raw_c = sum(x[2] for x in raw)
        if abs(raw_d - raw_c) > 0.005:
            # Genuinely unbalanced at source -- never plug it.
            problems.append(
                f'{bas_key}: unbalanced at source debit {raw_d:.3f} credit {raw_c:.3f}')
            return None

        # Balanced at source: round, then push the sub-halala residue onto the
        # largest line so the 2 dp representation balances too.  This is
        # decimal rounding of an exactly-balanced voucher, not a plug.
        for x in raw:
            x[1], x[2] = round(x[1], 2), round(x[2], 2)
        resid = round(sum(x[1] for x in raw) - sum(x[2] for x in raw), 2)
        if resid:
            side = 2 if resid > 0 else 1        # too much debit -> bump credit
            target = max(raw, key=lambda x: max(x[1], x[2]))
            target[side] = round(target[side] + abs(resid), 2)

        items = [(0, 0, {'account_id': a, 'debit': d, 'credit': c,
                         'name': f'BAS {ftype}/{ftype2} {number1:.0f}'})
                 for a, d, c in raw]

        return {
            'move_type': 'entry',
            'journal_id': self._journal(ftype, company).id,
            'company_id': company.id,
            'date': lines[0]['FDATE'].date() if lines[0]['FDATE'] else self.date_from,
            'ref': f'BAS {ftype}/{ftype2} br{code2} #{number1:.0f}',
            'x_bas_key': bas_key,
            'x_bas_ftype': f'{ftype}/{ftype2}',
            'x_bas_branch': code2,
            'line_ids': items,
        }

    def _flush_moves(self, Move, batch):
        """Create + post one batch. Named _flush_moves, not _flush: the ORM
        already defines BaseModel._flush and shadowing it breaks the call."""
        moves = Move.create(batch)
        moves._post(soft=False)
        return len(moves)

    # ------------------------------------------------------------------
    # Bilingual names
    # ------------------------------------------------------------------
    # BAS carries BOTH names: DNAME (Arabic, effectively 100% populated) and
    # DNAME2 (English, real on 1,872 of 4,402 -- the rest just repeat the
    # Arabic).  ``account.account.name`` and ``account.group.name`` are
    # translatable jsonb in Odoo 19, so we do not have to choose: Arabic goes
    # in ``ar_001``, English in ``en_US``, and each accountant reads the chart
    # in their own language off the same record.
    #
    # Where BAS has no real English the en_US value falls back to the Arabic
    # rather than being left blank -- a blank name would make the account
    # unreadable in English, which is worse than showing the Arabic next to
    # the (language-neutral) code.
    # The chart is displayed in **Arabic in every language by default**, and
    # that is deliberate.  BAS has real English on only 1,872 of 4,402 accounts
    # (roots 100%, groups 64%, leaves 43%), so applying it would produce a chart
    # that is English for 43% of lines and Arabic for the rest -- harder to read
    # than one consistent language, and the accountants read Arabic.
    #
    # Mohammed, 2026-09-05: "just apply the arabic version even in the english
    # version, and make the english version as a feature somewhere else to be
    # activated".
    #
    # The English names are NOT discarded -- every one BAS has is kept on
    # ``account.account.x_bas_name_en`` and can be switched on at any time with
    # ``action_activate_english_names()``, and switched back off again.  Nothing
    # is ever machine-translated: an account with no English in BAS keeps its
    # Arabic name in both locales.
    _LANG_PARAM = 'ksw_bas.chart_language'      # 'ar' (default) | 'en'

    @api.model
    def _bas_names(self):
        """{code: (arabic, english_or_empty)} straight from COD10."""
        conn = self._bas_connect()
        cur = conn.cursor(as_dict=True)
        cur.execute("""
            SELECT RTRIM(DCODE1) code, DNAME, DNAME2
            FROM COD10 WHERE DCODE1 IS NOT NULL AND RTRIM(DCODE1) <> ''
        """)
        out = {}
        for r in cur.fetchall():
            ar = (r['DNAME'] or '').strip()
            en = (r['DNAME2'] or '').strip()
            # DNAME2 repeats the Arabic on 2,527 rows -- that is not English.
            out[(r['code'] or '').strip()] = (ar, en if (en and en != ar) else '')
        conn.close()
        return out

    @api.model
    def action_sync_names(self, company=None, english=None):
        """Write names onto the chart.

        ``english=False`` (the default) puts the BAS Arabic in both locales.
        ``english=True`` puts BAS's English in ``en_US`` where it exists,
        falling back to Arabic where it does not.
        """
        if english is None:
            english = self.env['ir.config_parameter'].sudo().get_param(
                self._LANG_PARAM, 'ar') == 'en'
        bas = self._bas_names()
        done = {'accounts': 0, 'groups': 0, 'english_applied': 0}
        for model, key in (('account.account', 'accounts'), ('account.group', 'groups')):
            for rec in self.env[model].search([('x_bas_code', '!=', False)]):
                pair = bas.get(rec.x_bas_code)
                if not pair or not pair[0]:
                    continue
                ar, en = pair
                use_en = en if (english and en) else ar
                rec.update_field_translations('name', {'en_US': use_en, 'ar_001': ar})
                if model == 'account.account':
                    rec.x_bas_name_ar = ar
                    rec.x_bas_name_en = en or False
                if english and en:
                    done['english_applied'] += 1
                done[key] += 1
        self.env['ir.config_parameter'].sudo().set_param(
            self._LANG_PARAM, 'en' if english else 'ar')
        _logger.info('BAS name sync (english=%s): %s', english, done)
        return done

    def action_activate_english_names(self):
        """Switch the chart's en_US names to BAS's English where it exists."""
        return self.action_sync_names(company=self.company_id, english=True)

    def action_use_arabic_names(self):
        """Back to Arabic in every language (the default)."""
        return self.action_sync_names(company=self.company_id, english=False)

    @api.model
    def action_export_missing_english(self, path='/tmp/bas_missing_english.csv'):
        """CSV of accounts with no English name, for the accountants to fill in."""
        import csv
        recs = self.env['account.account'].search(
            [('x_bas_code', '!=', False), ('x_bas_name_en', '=', False)], order='code')
        with open(path, 'w', newline='', encoding='utf-8-sig') as fh:
            w = csv.writer(fh)
            w.writerow(['bas_code', 'arabic_name', 'english_name_TO_FILL', 'account_type'])
            for r in recs:
                w.writerow([r.x_bas_code, r.x_bas_name_ar or '', '', r.account_type])
        return {'path': path, 'rows': len(recs)}

    # ------------------------------------------------------------------
    # Partners
    # ------------------------------------------------------------------
    # BAS has no partner master: a customer *is* a leaf account under 1203 and
    # a supplier a leaf account under 2101.  We mirror them onto res.partner
    # using Odoo's native customer_rank / supplier_rank markers -- never an
    # x_is_client boolean (see Odoo 19 Pitfalls #101 / the Workshop rebuild).
    @api.model
    def action_import_partners(self, company=None):
        company = company or self.env.company
        conn = self._bas_connect()
        cur = conn.cursor(as_dict=True)
        cur.execute("""
            SELECT RTRIM(DCODE1) code, DNAME, DNAME2, TAX_ID, DPHONE, DPHONE2,
                   DADDRESS, DADDRESS2, EMAIL, DCREDIT_LIMT
            FROM COD10
            WHERE DLEVEL = 5
              AND (RTRIM(DCODE1) LIKE '1203%' OR RTRIM(DCODE1) LIKE '2101%')
            ORDER BY DCODE1
        """)
        rows = cur.fetchall()
        conn.close()

        Partner = self.env['res.partner']
        existing = {p.x_bas_code: p for p in Partner.search([('x_bas_code', '!=', False)])}
        sa = self.env.ref('base.sa', raise_if_not_found=False)
        created = updated = 0
        for r in rows:
            code = (r['code'] or '').strip()
            is_cust = code.startswith('1203')
            name = (r['DNAME'] or r['DNAME2'] or code).strip()
            vat = (r['TAX_ID'] or '').strip()
            vals = {
                'name': name,
                'x_bas_code': code,
                'x_bas_name_ar': name,
                # base_vat enforces the ZATCA format (15 digits, first and last
                # "3").  Many BAS TAX_IDs do not satisfy it, and forcing them in
                # raises ValidationError.  Keep the raw value on x_bas_vat
                # always; populate `vat` only when it is genuinely valid, so the
                # invalid ones stay visible instead of being silently dropped.
                'x_bas_vat': vat or False,
                'vat': vat if _SA_VAT_RE.match(vat or '') else False,
                # Odoo 19 merged `mobile` into `phone`; DPHONE2 goes to comment.
                'phone': (r['DPHONE'] or '').strip() or False,
                'email': (r['EMAIL'] or '').strip() or False,
                'street': (r['DADDRESS'] or '').strip() or False,
                'country_id': sa.id if sa else False,
                'comment': (('Tel2: ' + (r['DPHONE2'] or '').strip())
                            if (r['DPHONE2'] or '').strip() else False),
                'company_type': 'company',
                'customer_rank': 1 if is_cust else 0,
                'supplier_rank': 0 if is_cust else 1,
            }
            p = existing.get(code)
            if p:
                p.write(vals)
                updated += 1
            else:
                existing[code] = Partner.create(vals)
                created += 1
        _logger.info('BAS partners: %s created, %s updated', created, updated)
        return {'created': created, 'updated': updated, 'total': len(rows)}

    # ------------------------------------------------------------------
    # Products (ITM10)
    # ------------------------------------------------------------------
    @api.model
    def action_import_products(self, company=None):
        """ITM10 -> product.product, so invoice lines can carry a real product."""
        conn = self._bas_connect()
        cur = conn.cursor(as_dict=True)
        cur.execute("""SELECT RTRIM(ICODE) code, IDSCR, IDSCR2, IUNIT, ISPRICE
                       FROM ITM10 WHERE ICODE IS NOT NULL AND RTRIM(ICODE) <> ''""")
        rows = cur.fetchall()
        conn.close()
        Product = self.env['product.product']
        existing = {p.x_bas_code: p for p in Product.search([('x_bas_code', '!=', False)])}
        created = 0
        for r in rows:
            code = (r['code'] or '').strip()
            if code in existing:
                continue
            ar = (r['IDSCR'] or '').strip()
            en = (r['IDSCR2'] or '').strip()
            name = ar or en or code
            p = Product.create({
                'name': name,
                'default_code': code,
                'x_bas_code': code,
                # 'consu' not 'service': these are goods. `is_storable` is not
                # set -- it comes from `stock`, which is not installed here.
                'type': 'consu',
                'list_price': float(r['ISPRICE'] or 0.0),
                'taxes_id': [(5, 0, 0)],   # tax comes from the BAS document, not the product
            })
            # Arabic is the source of truth (same rule as the chart); English only
            # where BAS genuinely has a different string.
            p.update_field_translations('name', {'en_US': ar or en or code,
                                                 'ar_001': ar or name})
            existing[code] = p
            created += 1
        _logger.info('BAS products: %s created of %s', created, len(rows))
        return {'created': created, 'total': len(rows)}

    # ------------------------------------------------------------------
    # Invoice conversion
    # ------------------------------------------------------------------
    # BAS debits the counterpart directly (cash 1201* for POS, the customer's
    # own 1203* leaf for credit sales).  Odoo posts an out_invoice to the
    # PARTNER's receivable, so to keep the ledger byte-identical to the entries
    # we already reconciled, each partner's receivable is set to the very BAS
    # account that partner represents -- including 15 "Cash Sales" partners,
    # one per cash account.  Same idea for payables on 2101*.
    @api.model
    def action_map_partner_accounts(self, company=None):
        company = company or self.env.company
        Partner = self.env['res.partner'].with_company(company)
        Account = self.env['account.account']
        acc_by_code = {a.x_bas_code: a for a in Account.search([('x_bas_code', '!=', False)])}
        n = 0
        for p in Partner.search([('x_bas_code', '!=', False)]):
            acc = acc_by_code.get(p.x_bas_code)
            if not acc:
                continue
            if p.x_bas_code.startswith('2101'):
                p.property_account_payable_id = acc.id
                # A vendor still needs a valid receivable default: Odoo falls
                # back to the company one, and if that has been archived every
                # document touching the partner dies with "account is
                # archived" -- 153 vendors did exactly that mid-run.
                if not p.property_account_receivable_id.active:
                    p.property_account_receivable_id = self._pos_transit_account(company).id
            elif acc.account_type in ('asset_cash', 'liability_credit_card'):
                # A till partner: invoices post to transit, the payment lands
                # the money in this actual BAS cash account.
                p.property_account_receivable_id = self._pos_transit_account(company).id
            else:
                p.property_account_receivable_id = acc.id
            n += 1

        # A partner for every non-1203 account BAS debits on a sales document.
        conn = self._bas_connect()
        cur = conn.cursor(as_dict=True)
        cur.execute("""
            SELECT DISTINCT RTRIM(FCODE) code FROM vou10
            WHERE FTYPE IN ('600','001','002','101')
              AND RTRIM(ISNULL(FCODE,'')) <> ''
              AND RTRIM(FCODE) NOT LIKE '1203%'
        """)
        codes = [(r['code'] or '').strip() for r in cur.fetchall()]
        conn.close()
        made = 0
        for code in codes:
            acc = acc_by_code.get(code)
            if not acc or Partner.search_count([('x_bas_code', '=', code)]):
                continue
            Partner.create({
                'name': acc.with_context(lang='ar_001').name,
                'x_bas_code': code,
                'x_bas_name_ar': acc.x_bas_name_ar,
                'company_type': 'company',
                'customer_rank': 1,
                'property_account_receivable_id': (
                    self._pos_transit_account(company).id
                    if acc.account_type in ('asset_cash', 'liability_credit_card')
                    else acc.id),
            })
            made += 1
        _logger.info('BAS partner accounts: %s mapped, %s counterpart partners created', n, made)
        return {'mapped': n, 'created': made}

    # Only two BAS types are customer sales documents:
    #   001 بيع نقدا                  -- cash sale, debit side is the TILL on
    #                                    6,190 of 6,193 docs -> invoice + payment
    #   002 بيع اجل لاذونات تسليم      -- the monthly invoice raised against
    #                                    accumulated delivery notes -> invoice
    # Deliberately NOT here:
    #   600  delivery notes, post nothing at all (excluded from the import)
    #   101  DR expense_direct_cost / CR inventory -- a cost movement, not a sale
    #   018/015/006  receipts and proformas -- stay as journal entries
    # The move_type is still derived from the data, never from this dict.
    _SALE_FTYPES = {'001': 'out_invoice', '002': 'out_invoice'}

    def _sale_taxes(self, company):
        """(15% tax, 0% tax) on the sale side, both posting to BAS's VAT account."""
        T = self.env['account.tax']
        t15 = T.search([('company_id', '=', company.id), ('type_tax_use', '=', 'sale'),
                        ('amount', '=', 15.0), ('amount_type', '=', 'percent')], limit=1)
        t0 = T.search([('company_id', '=', company.id), ('type_tax_use', '=', 'sale'),
                       ('amount', '=', 0.0)], limit=1)
        return t15, t0

    @api.model
    def action_convert_invoices(self, date_from, date_to, company=None,
                                branch=None, limit_docs=None):
        """Turn the BAS sales *entries* into native Odoo invoices.

        Conversion, never addition: the `entry` created from vou10 is deleted
        and replaced by an out_invoice/out_refund carrying the same x_bas_key,
        so the ledger total is unchanged and nothing is counted twice.
        """
        company = company or self.env.company
        t15, t0 = self._sale_taxes(company)
        accounts = {a.x_bas_code: a for a in self.env['account.account'].search(
            [('x_bas_code', '!=', False)])}
        partners = {p.x_bas_code: p for p in self.env['res.partner'].search(
            [('x_bas_code', '!=', False)])}
        products = {p.x_bas_code: p for p in self.env['product.product'].search(
            [('x_bas_code', '!=', False)])}

        conn = self._bas_connect()
        cur = conn.cursor(as_dict=True)
        args = [date_from, date_to]
        bsql = ''
        if branch:
            bsql = ' AND RTRIM(CODE2) = %s'
            args.append(branch)
        cur.execute(f"""
            SELECT FTYPE, FTYPE2, RTRIM(CODE2) CODE2, NUMBER1, SERIAL,
                   RTRIM(ISNULL(FCODE,'')) FCODE, RTRIM(ISNULL(TCODE,'')) TCODE,
                   AMOUNT, BAMOUNT, TAX_AMOUNT, FDATE
            FROM vou10
            WHERE FDATE >= %s AND FDATE < DATEADD(day, 1, %s){bsql}
              AND FTYPE IN ('600','001','002','101')
            ORDER BY FTYPE, FTYPE2, CODE2, NUMBER1, SERIAL
        """, tuple(args))
        vou = defaultdict(list)
        for r in cur.fetchall():
            vou[(r['FTYPE'], r['FTYPE2'], r['CODE2'], r['NUMBER1'])].append(r)

        # STR10 carries real product lines, but only for FTYPE 600.
        # EXISTS, never JOIN: vou10 holds one row per journal LINE (a debit and
        # a credit per voucher), so joining STR10 to it fans every product line out
        # once per voucher line and silently DOUBLES every POS invoice.
        cur.execute(f"""
            SELECT s.FTYPE, s.FTYPE2, RTRIM(s.CODE2) CODE2, s.NUMBER1,
                   RTRIM(s.ICODE) ICODE, s.IDSCR, s.QUAN, s.AMOUNT, s.TAX_AMOUNT
            FROM STR10 s
            WHERE EXISTS (
                SELECT 1 FROM vou10 v
                WHERE v.FTYPE=s.FTYPE AND v.FTYPE2=s.FTYPE2
                  AND v.CODE2=s.CODE2 AND v.NUMBER1=s.NUMBER1
                  AND v.FDATE >= %s AND v.FDATE < DATEADD(day, 1, %s){bsql}
            )
        """, tuple(args))
        str10 = defaultdict(list)
        for r in cur.fetchall():
            str10[(r['FTYPE'], r['FTYPE2'], r['CODE2'], r['NUMBER1'])].append(r)
        conn.close()

        self = self.with_context(bas_journal_cache={}, bas_cash_journals={})
        Move = self.env['account.move'].with_context(
            tracking_disable=True).with_company(company)

        # One search for the whole range, not one per document.
        keys = list(vou)[:limit_docs] if limit_docs else list(vou)
        bas_keys = [f'{k[0]}/{k[1]}/{k[2]}/{k[3]:.0f}' for k in keys]
        old_by_key = {}
        for chunk in (bas_keys[i:i + 2000] for i in range(0, len(bas_keys), 2000)):
            for m in Move.search([('x_bas_key', 'in', chunk),
                                  ('move_type', '=', 'entry')]):
                old_by_key[m.x_bas_key] = m

        converted = skipped = 0
        problems = []
        batch_vals, batch_old, batch_pay = [], Move.browse(), []

        def flush():
            nonlocal batch_vals, batch_old, batch_pay, converted
            if not batch_vals:
                return
            # Delete the entries first: the invoice reuses their x_bas_key,
            # which is UNIQUE.
            if batch_old:
                batch_old.button_draft()
                batch_old.with_context(force_delete=True).unlink()
            moves = Move.create(batch_vals)
            moves._post(soft=False)
            self._settle_cash_invoices(moves, batch_pay, company)
            converted += len(moves)
            batch_vals, batch_old, batch_pay = [], Move.browse(), []
            self.env.cr.commit()

        for key in keys:
            ftype, ftype2, code2, number1 = key
            bas_key = f'{ftype}/{ftype2}/{code2}/{number1:.0f}'
            old = old_by_key.get(bas_key)
            if not old:
                continue                      # already converted, or never imported
            # Journal follows the document type: a 002 monthly invoice must not
            # land in the POS journal just because the converter once handled 600.
            vals, cash_acc = self._build_invoice(
                bas_key, key, vou[key], str10.get(key), accounts, partners,
                products, t15, t0, self._journal(ftype, company), company, problems)
            if vals is None:
                skipped += 1
                continue
            batch_vals.append(vals)
            batch_old |= old
            batch_pay.append(cash_acc)
            if len(batch_vals) >= 200:
                flush()
        flush()

        _logger.info('BAS invoice conversion %s..%s: %s converted, %s skipped',
                     date_from, date_to, converted, skipped)
        return {'converted': converted, 'skipped': skipped,
                'problems': problems[:50]}

    def _build_invoice(self, bas_key, key, vlines, slines, accounts, partners,
                       products, t15, t0, journal, company, problems):
        ftype, ftype2, code2, number1 = key
        if ftype not in self._SALE_FTYPES:
            return None, None
        use_b = ftype in _BAMOUNT_FTYPES

        # Direction comes from the DATA, never from the document-type code.
        # BAS books FTYPE 002/101 ("returns") in the SAME orientation as a sale
        # -- debit the counterpart, credit revenue -- so mapping them to
        # out_refund by FTYPE inverted the sign on every line of 2,300+
        # documents.  Counterpart debited => invoice; counterpart credited =>
        # refund.
        rev_codes = {(r['TCODE'] or '').strip() for r in vlines if r['TCODE']}
        rev_codes |= {(r['FCODE'] or '').strip() for r in vlines if r['FCODE']}
        counterpart_debited = any(
            r['FCODE'] and accounts.get((r['FCODE'] or '').strip()) is not None
            and accounts[(r['FCODE'] or '').strip()].account_type not in
            ('income', 'income_other') for r in vlines)
        move_type = 'out_invoice' if counterpart_debited else 'out_refund'

        # The debit side is the counterpart BAS chose (cash 1201*, or the
        # customer's own 1203* leaf).  That account IS the partner's receivable,
        # so Odoo's own posting lands exactly where BAS put it.
        dr = [r for r in vlines if r['FCODE']]
        if not dr:
            problems.append(f'{bas_key}: no debit line')
            return None, None
        partner = partners.get(dr[0]['FCODE'])
        if not partner:
            problems.append(f'{bas_key}: no partner for {dr[0]["FCODE"]}')
            return None, None
        # Odoo's _check_payable_receivable requires the counterpart line of an
        # invoice to sit on a receivable account (it stamps a due date on it).
        # BAS debits CASH directly for POS sales, and a cash account cannot
        # carry a due date -- those documents need the native invoice+payment
        # pair instead, which is a separate pass.  Skip them here rather than
        # retyping a cash account as receivable, which would corrupt the
        # balance sheet.
        # A cash/bank counterpart cannot carry a due date, so the invoice goes
        # to the POS transit receivable and a payment then moves it to BAS's own
        # cash account -- Odoo's native invoice+payment shape for a cash sale.
        counterpart = accounts.get(dr[0]['FCODE'])
        if not counterpart:
            problems.append(f'{bas_key}: unknown counterpart {dr[0]["FCODE"]}')
            return None, None
        cash_acc = counterpart if counterpart.account_type in (
            'asset_cash', 'liability_credit_card') else None
        # Only a genuine customer receivable or a till can back a sales
        # invoice.  A voucher booked against a SUPPLIER account (2101*) is an
        # adjustment, not a sale: forcing it into an invoice sent it to the
        # default receivable 102011 instead of the BAS account.  Leave those as
        # journal entries -- which is what BAS effectively holds for them.
        if not cash_acc and counterpart.account_type != 'asset_receivable':
            problems.append(
                f'{bas_key}: counterpart {dr[0]["FCODE"]} is '
                f'{counterpart.account_type} -- left as a journal entry')
            return None, None
        # For a receivable counterpart the partner MUST post to that very
        # account, or the money lands in Odoo's default instead of BAS's.
        if not cash_acc:
            rec = partner.with_company(company).property_account_receivable_id
            if rec != counterpart:
                partner.with_company(company).property_account_receivable_id = counterpart.id

        # Revenue account comes from BAS's own credit line, not from the
        # product or the journal default -- that is what keeps each sale in the
        # exact 4101*/4102* account BAS used.
        rev = None
        for r in vlines:
            if r['TCODE'] and r['TCODE'] != _VAT_OUTPUT_CODE:
                a = accounts.get(r['TCODE'])
                if a and a.account_type in ('income', 'income_other'):
                    rev = a
                    break
        if rev is None:
            problems.append(f'{bas_key}: no revenue account on the voucher')
            return None, None

        # Whether VAT applies is decided by the GL (vou10), NOT by the STR10
        # line.  They disagree on branch 172's 600/3 sales -- vou10 TAX=0 while
        # STR10 TAX=87.75 -- and vou10 is what was reconciled and filed, so it
        # is authoritative here.  Trusting STR10 inflated 89 documents by
        # exactly 15% on a single day.
        voucher_tax = sum(round(float(r['TAX_AMOUNT'] or 0.0), 2) for r in vlines if r['TCODE'])
        taxed = bool(voucher_tax)

        # A voucher may credit SEVERAL revenue accounts (2,795 of them in
        # Jul-Sep).  Collapsing every STR10 product line onto the first income
        # account keeps the document balanced and silently redistributes revenue
        # between accounts -- 34m SAR of misstatement.  When there is more than
        # one revenue account, build from the vou10 credits so each account
        # keeps its own amount; STR10 product detail is only safe when the
        # voucher credits exactly one.
        rev_accounts = []
        for r in vlines:
            if not r['TCODE']:
                continue
            a = accounts.get((r['TCODE'] or '').strip())
            if a and a.account_type in ('income', 'income_other') and a not in rev_accounts:
                rev_accounts.append(a)
        if len(rev_accounts) != 1:
            slines = None            # fall back to per-credit-line construction

        lines = []
        if slines:
            # POS: real product lines straight from STR10.
            for s in slines:
                net = round(float(s['AMOUNT'] or 0.0), 2)
                qty = float(s['QUAN'] or 0.0) or 1.0
                if not net:
                    continue
                prod = products.get(s['ICODE'])
                lines.append((0, 0, {
                    'product_id': prod.id if prod else False,
                    'name': (s['IDSCR'] or (prod.name if prod else s['ICODE']) or '/').strip(),
                    'quantity': qty,
                    'price_unit': net / qty if qty else net,
                    'account_id': rev.id,
                    'tax_ids': [(6, 0, [(t15 if taxed else t0).id])] if (t15 and t0) else False,
                }))
        else:
            # No STR10 (standard invoice / return): one line per revenue credit.
            for r in vlines:
                if not r['TCODE']:
                    continue
                acc = accounts.get(r['TCODE'])
                if not acc:
                    problems.append(f'{bas_key}: unknown revenue account {r["TCODE"]}')
                    return None, None
                if acc.x_bas_code == _VAT_OUTPUT_CODE:
                    continue                    # VAT comes from the tax, not a line
                net = round(float((r['BAMOUNT'] if use_b else r['AMOUNT']) or 0.0), 2)
                if not net:
                    continue
                lines.append((0, 0, {
                    'name': acc.with_context(lang='ar_001').name or '/',
                    'quantity': 1.0,
                    'price_unit': net,
                    'account_id': acc.id,
                    'tax_ids': [(6, 0, [(t15 if taxed else t0).id])] if (t15 and t0) else False,
                }))
        if not lines:
            problems.append(f'{bas_key}: no invoice lines')
            return None, None

        return {
            'move_type': move_type,
            'partner_id': partner.id,
            'journal_id': journal.id,
            'company_id': company.id,
            'invoice_date': vlines[0]['FDATE'].date() if vlines[0]['FDATE'] else False,
            'date': vlines[0]['FDATE'].date() if vlines[0]['FDATE'] else False,
            'ref': f'BAS {ftype}/{ftype2} br{code2} #{number1:.0f}',
            'x_bas_key': bas_key,
            'x_bas_ftype': f'{ftype}/{ftype2}',
            'x_bas_branch': code2,
            'invoice_line_ids': lines,
        }, cash_acc

    # ------------------------------------------------------------------
    # Cash sales: the native invoice + payment pair
    # ------------------------------------------------------------------
    # BAS books a POS sale straight to cash.  Odoo cannot: an invoice's
    # counterpart must be a receivable (it carries the due date).  So a cash
    # sale becomes what Odoo would itself have produced -- an invoice to a POS
    # transit receivable, then a payment in that till's own journal moving it to
    # BAS's cash account.  Transit nets to zero, so ACCOUNT BALANCES still match
    # BAS exactly; only turnover is higher, which is inherent to the native shape.
    _TRANSIT_CODE = 'POSTRANSIT'

    @api.model
    def _pos_transit_account(self, company):
        A = self.env['account.account']
        acc = A.search([('code', '=', self._TRANSIT_CODE)], limit=1)
        if not acc:
            acc = A.create({
                'code': self._TRANSIT_CODE,
                'name': 'POS Cash Transit',
                'account_type': 'asset_receivable',
                'reconcile': True,
                'company_ids': [(4, company.id)],
            })
            acc.update_field_translations('name', {
                'en_US': 'POS Cash Transit',
                'ar_001': 'حساب وسيط المبيعات النقدية'})
        return acc

    def _cash_journal(self, cash_acc, company):
        """One journal per BAS cash account -- i.e. one till, as Odoo models it."""
        cache = self.env.context.get('bas_cash_journals')
        if cache is not None and cash_acc.id in cache:
            return self.env['account.journal'].browse(cache[cash_acc.id])
        J = self.env['account.journal']
        code = ('C' + (cash_acc.x_bas_code or '')[-4:])[:5]
        j = J.search([('company_id', '=', company.id), ('code', '=', code)], limit=1)
        if not j:
            j = J.create({
                'name': cash_acc.with_context(lang='ar_001').name or code,
                'code': code, 'type': 'cash', 'company_id': company.id,
                'default_account_id': cash_acc.id,
            })

        # Point the outstanding account AT the till, so the payment posts
        # debit till / credit receivable in one move -- exactly how BAS booked
        # the cash.  Setting it to False does NOT do this: Odoo then falls back
        # to "Outstanding Receipts" and the money never reaches the till
        # (36,069.86 sat there on the first attempt).  Applied to journals we
        # just made AND ones left from an earlier run.
        (j.inbound_payment_method_line_ids
         | j.outbound_payment_method_line_ids).payment_account_id = cash_acc.id
        if cache is not None:
            cache[cash_acc.id] = j.id
        return j

    def _settle_cash_invoices(self, moves, cash_accs, company):
        """Pay the cash-sale invoices from their own till, then reconcile."""
        Payment = self.env['account.payment'].with_context(
            tracking_disable=True).with_company(company)
        pay_vals, targets = [], []
        for move, cash_acc in zip(moves, cash_accs):
            if not cash_acc or move.move_type not in ('out_invoice', 'out_refund'):
                continue
            pay_vals.append({
                'payment_type': 'inbound' if move.move_type == 'out_invoice' else 'outbound',
                'partner_type': 'customer',
                'partner_id': move.partner_id.id,
                'amount': abs(move.amount_total),
                'date': move.date,
                'journal_id': self._cash_journal(cash_acc, company).id,
                'company_id': company.id,
                'memo': move.ref,
            })
            targets.append(move)
        if not pay_vals:
            return
        payments = Payment.create(pay_vals)
        payments.action_post()
        for pay, move in zip(payments, targets):
            lines = (pay.move_id.line_ids + move.line_ids).filtered(
                lambda l: l.account_id.account_type == 'asset_receivable'
                and not l.reconciled)
            if len(lines) > 1:
                lines.reconcile()

    # ------------------------------------------------------------------
    # Opening balances
    # ------------------------------------------------------------------
    # Importing vou10 gives 2026 MOVEMENT only.  Without the opening balances
    # the P&L is right (it starts at zero each year) but every balance-sheet
    # account is wrong: no capital, no retained earnings, no fixed assets, no
    # brought-forward AR/AP.  COD10.DOLDACC carries them, and they balance
    # exactly: assets -201,771,314.75 vs liabilities+equity +201,771,314.75,
    # sum -0.00, with zero P&L accounts carrying an opening (as it should be).
    #
    # Sign convention is CREDIT-POSITIVE: assets come through negative.
    @api.model
    def action_import_opening_balances(self, company=None, date='2026-01-01'):
        company = company or self.env.company
        Move = self.env['account.move'].with_context(
            tracking_disable=True, check_move_validity=False).with_company(company)
        key = f'OPENING/{date[:4]}'
        if Move.search_count([('x_bas_key', '=', key)]):
            return {'skipped': 'already imported'}

        accounts = {a.x_bas_code: a for a in self.env['account.account'].search(
            [('x_bas_code', '!=', False)])}
        conn = self._bas_connect()
        cur = conn.cursor(as_dict=True)
        cur.execute("""SELECT RTRIM(DCODE1) code, DOLDACC FROM COD10
                       WHERE DLEVEL = 5 AND DOLDACC <> 0""")
        rows = cur.fetchall()
        conn.close()

        lines, debit, credit, missing = [], 0.0, 0.0, []
        for r in rows:
            acc = accounts.get((r['code'] or '').strip())
            if not acc:
                missing.append(r['code'])
                continue
            v = round(float(r['DOLDACC'] or 0.0), 2)
            if not v:
                continue
            d, c = (0.0, v) if v > 0 else (-v, 0.0)
            debit += d
            credit += c
            lines.append((0, 0, {
                'account_id': acc.id, 'debit': d, 'credit': c,
                'name': 'Opening balance %s' % date[:4],
            }))
        if round(debit - credit, 2):
            raise UserError(_('Opening balances do not balance: %(d).2f vs %(c).2f',
                              d=debit, c=credit))
        journal = self._journal('OPEN', company)
        move = Move.create({
            'move_type': 'entry', 'journal_id': journal.id,
            'company_id': company.id, 'date': date,
            'ref': 'BAS opening balances %s' % date[:4],
            'x_bas_key': key, 'x_bas_ftype': 'OPENING',
            'line_ids': lines,
        })
        move._post(soft=False)
        _logger.info('BAS opening balances: %s lines, %.2f each side', len(lines), debit)
        return {'lines': len(lines), 'debit': debit, 'credit': credit,
                'missing_accounts': missing[:10]}

    # ------------------------------------------------------------------
    # Inventory
    # ------------------------------------------------------------------
    # Mohammed, 2026-09-06: water is a PRODUCT (it must be invoiceable) but is
    # NOT stock-tracked -- it is produced to order and delivered the same day, so
    # tracking it would create ~281,000 movements a year for no benefit. Spares
    # and consumables ARE tracked, which mirrors the only real stock practice BAS
    # already has: 31004 ممبرين سي وتر shows purchased 300 / used 260 / on hand 63.
    #
    # Classification is by BAS item-code prefix, confirmed against the units:
    #   15xx  plant spares  (pumps, seals, valves)      33 items, unit حبه
    #   31xx  RO membranes                               5 items, unit حبه
    #   33xx  vehicle spares (tyres etc.)               57 items, unit حبه
    # Everything else is water (cubic metres) or a service (trip / day / hour).
    _STOCKABLE_PREFIXES = ('15', '31', '33')

    @api.model
    def action_setup_inventory(self, company=None):
        company = company or self.env.company
        Product = self.env['product.product']
        products = Product.search([('x_bas_code', '!=', False)])
        storable = products.filtered(
            lambda p: (p.x_bas_code or '').startswith(self._STOCKABLE_PREFIXES))
        rest = products - storable
        storable.product_tmpl_id.write({'is_storable': True})
        rest.product_tmpl_id.write({'is_storable': False})
        _logger.info('BAS inventory: %s storable, %s not', len(storable), len(rest))
        return {'storable': len(storable), 'not_storable': len(rest)}

    @api.model
    def action_load_opening_stock(self, company=None):
        """Opening quantities from ITM10.IAVAILQ, for the storable items only.

        Only POSITIVE balances are loaded. A negative IAVAILQ on a water item is
        cumulative sales with no receipts ever recorded -- it is not a stock
        figure and must not become one.
        """
        company = company or self.env.company
        wh = self.env['stock.warehouse'].search([('company_id', '=', company.id)], limit=1)
        if not wh:
            raise UserError(_('No warehouse for %s', company.name))
        products = {p.x_bas_code: p for p in self.env['product.product'].search(
            [('x_bas_code', '!=', False), ('is_storable', '=', True)])}
        if not products:
            raise UserError(_('Run action_setup_inventory first.'))

        conn = self._bas_connect()
        cur = conn.cursor(as_dict=True)
        cur.execute("SELECT RTRIM(ICODE) code, IAVAILQ FROM ITM10 WHERE IAVAILQ > 0")
        rows = cur.fetchall()
        conn.close()

        Quant = self.env['stock.quant'].with_context(inventory_mode=True)
        made = 0
        skipped = []
        for r in rows:
            p = products.get((r['code'] or '').strip())
            if not p:
                skipped.append((r['code'] or '').strip())
                continue
            q = Quant.create({
                'product_id': p.id,
                'location_id': wh.lot_stock_id.id,
                'inventory_quantity': float(r['IAVAILQ'] or 0.0),
            })
            q.action_apply_inventory()
            made += 1
        _logger.info('BAS opening stock: %s items loaded', made)
        return {'loaded': made, 'not_storable_skipped': len(skipped)}

    # ------------------------------------------------------------------
    # Delivery notes  (FTYPE 600)
    # ------------------------------------------------------------------
    # A BAS 600 is a delivery note: it posts NOTHING to the ledger (proven by
    # three statements of account) and belongs in Odoo as a stock.picking, not an
    # invoice.  The water products are deliberately non-storable, so these moves
    # carry no stock or valuation -- their value is the delivery -> monthly
    # invoice chain, which is exactly how the business already works.
    @api.model
    def action_import_delivery_notes(self, date_from, date_to, company=None,
                                     limit_docs=None):
        company = company or self.env.company
        wh = self.env['stock.warehouse'].search([('company_id', '=', company.id)], limit=1)
        if not wh:
            raise UserError(_('No warehouse for %s', company.name))
        ptype = wh.out_type_id
        dest = ptype.default_location_dest_id or self.env.ref('stock.stock_location_customers')
        src = ptype.default_location_src_id or wh.lot_stock_id

        partners = {p.x_bas_code: p for p in self.env['res.partner'].search(
            [('x_bas_code', '!=', False)])}
        products = {p.x_bas_code: p for p in self.env['product.product'].search(
            [('x_bas_code', '!=', False)])}

        conn = self._bas_connect()
        cur = conn.cursor(as_dict=True)
        cur.execute("""
            SELECT FTYPE, FTYPE2, RTRIM(CODE2) CODE2, NUMBER1,
                   RTRIM(ISNULL(FCODE,'')) FCODE, FDATE
            FROM vou10
            WHERE FTYPE = '600' AND FDATE >= %s AND FDATE < DATEADD(day, 1, %s)
              AND RTRIM(ISNULL(FCODE,'')) <> ''
        """, (date_from, date_to))
        heads = {}
        for r in cur.fetchall():
            heads[(r['FTYPE'], r['FTYPE2'], r['CODE2'], r['NUMBER1'])] = r
        # EXISTS, never JOIN -- vou10 holds one row per journal line, so a join
        # duplicates every STR10 line once per voucher line (see pitfall #116).
        cur.execute("""
            SELECT s.FTYPE, s.FTYPE2, RTRIM(s.CODE2) CODE2, s.NUMBER1,
                   RTRIM(s.ICODE) ICODE, s.IDSCR, s.QUAN
            FROM STR10 s
            WHERE EXISTS (SELECT 1 FROM vou10 v
                          WHERE v.FTYPE=s.FTYPE AND v.FTYPE2=s.FTYPE2
                            AND v.CODE2=s.CODE2 AND v.NUMBER1=s.NUMBER1
                            AND v.FTYPE='600'
                            AND v.FDATE >= %s AND v.FDATE < DATEADD(day, 1, %s))
        """, (date_from, date_to))
        lines = defaultdict(list)
        for r in cur.fetchall():
            lines[(r['FTYPE'], r['FTYPE2'], r['CODE2'], r['NUMBER1'])].append(r)
        conn.close()

        # skip_sms: stock_sms intercepts button_validate with a "send SMS to the
        # customer?" wizard whenever the partner has a phone number -- it returns
        # an action instead of validating, so the picking silently stays
        # 'assigned'.  A historical import must never text customers about a
        # delivery that happened weeks ago.
        Picking = self.env['stock.picking'].with_context(
            tracking_disable=True, skip_sms=True)
        existing = set(Picking.search([('x_bas_key', '!=', False)]).mapped('x_bas_key'))

        made = skipped = 0
        problems, batch = [], []
        keys = list(heads)[:limit_docs] if limit_docs else list(heads)
        for key in keys:
            ftype, ftype2, code2, number1 = key
            bas_key = f'{ftype}/{ftype2}/{code2}/{number1:.0f}'
            if bas_key in existing:
                continue
            h = heads[key]
            partner = partners.get(h['FCODE'])
            if not partner:
                problems.append(f'{bas_key}: no partner for {h["FCODE"]}')
                skipped += 1
                continue
            moves = []
            for l in lines.get(key, []):
                prod = products.get(l['ICODE'])
                if not prod:
                    continue
                qty = abs(float(l['QUAN'] or 0.0))
                if not qty:
                    continue
                # Odoo 19: stock.move has no `name`; the printed text is
                # `description_picking`, and `date` + `company_id` are required.
                moves.append((0, 0, {
                    'description_picking': (l['IDSCR'] or prod.name or '/').strip()[:60],
                    'product_id': prod.id, 'product_uom_qty': qty,
                    'product_uom': prod.uom_id.id,
                    'date': h['FDATE'], 'company_id': company.id,
                    'location_id': src.id, 'location_dest_id': dest.id,
                }))
            if not moves:
                problems.append(f'{bas_key}: no deliverable lines')
                skipped += 1
                continue
            batch.append({
                'partner_id': partner.id, 'picking_type_id': ptype.id,
                'location_id': src.id, 'location_dest_id': dest.id,
                'scheduled_date': h['FDATE'], 'date_done': h['FDATE'],
                'origin': f'BAS {ftype}/{ftype2} br{code2} #{number1:.0f}',
                'company_id': company.id,
                'x_bas_key': bas_key, 'x_bas_branch': code2,
                'move_ids': moves,   # Odoo 19 removed move_ids_without_package
            })
            if len(batch) >= 200:
                made += self._flush_pickings(Picking, batch)
                batch = []
        if batch:
            made += self._flush_pickings(Picking, batch)
        _logger.info('BAS delivery notes %s..%s: %s created, %s skipped',
                     date_from, date_to, made, skipped)
        return {'created': made, 'skipped': skipped, 'problems': problems[:20]}

    def _flush_pickings(self, Picking, batch):
        """Create a batch of delivery notes and mark them delivered.

        These are historical: they must end up `done`, not `draft`, and they must
        keep their ORIGINAL date. `button_validate` stamps `date_done` with now,
        so it is written back afterwards -- otherwise every 2026 delivery looks
        like it happened on the day of the import.
        """
        picks = Picking.create(batch)
        picks.action_confirm()
        for p in picks:
            for m in p.move_ids:
                m.quantity = m.product_uom_qty
                m.picked = True
        picks.button_validate()
        for p, vals in zip(picks, batch):
            p.write({'date_done': vals['date_done']})
        self.env.cr.commit()
        return len(picks)

    # ------------------------------------------------------------------
    # Fixed asset register
    # ------------------------------------------------------------------
    # BAS has NO asset register: `FIX`/`FIXDATA`/`EFIX` are report-layout
    # metadata, not assets.  What exists is 733 leaf accounts under 19xx paired
    # with 733 accumulated-depreciation accounts under 24xx (561 match 1:1 by
    # suffix), each account BEING one named asset.
    #
    # So cost and accumulated depreciation per asset are recoverable, and net book
    # value with them.  What BAS holds NOWHERE is acquisition date, useful life or
    # depreciation method -- depreciation is posted by hand as 33xx vouchers.
    #
    # Therefore the assets are created as a REGISTER, in draft, with no
    # depreciation schedule.  Inventing a life would be inventing data.  The
    # ledger already carries the correct per-account balances from the opening
    # entry; this gives the register a person can read.
    _ASSET_GROUPS = {
        '1901': 'Land',
        '1902': 'Buildings & Portacabins',
        '1903': 'Desalination Machinery',
        '1904': 'Furniture & Fittings',
        '1905': 'Tools & Equipment',
        '1906': 'Vehicles',
    }

    @staticmethod
    def _norm_asset_name(name):
        import re
        n = (name or '').strip().replace('مجمع اهلاك', '').replace('مجمع إهلاك', '')
        return re.sub(r'\s+', ' ', n).strip()

    def _depreciation_map(self):
        """{asset 19xx code: accumulated depreciation} consuming each 24xx ONCE.

        Matching per-asset in a loop double-counts: a 24xx account that is the
        suffix pair of one asset can also be the name match of another, and both
        then claim it -- that overstated the register by 8,065,717.36. The map is
        therefore built globally: suffix pairs are authoritative and claimed
        first, and only the leftovers are offered to name matching.
        """
        Account = self.env['account.account']
        assets = Account.search([('x_bas_code', '=like', '19%')])
        deps = Account.search([('x_bas_code', '=like', '24%')])
        asset_codes = {a.x_bas_code for a in assets}

        def bal(a):
            self.env.cr.execute(
                "SELECT COALESCE(SUM(l.debit-l.credit),0) "
                "FROM account_move_line l WHERE l.account_id=%s", (a.id,))
            return float(self.env.cr.fetchone()[0] or 0.0)

        out, claimed = {}, set()
        for d in deps:                      # pass 1: exact suffix
            target = '19' + d.x_bas_code[2:]
            if target in asset_codes:
                amt = -bal(d)
                if amt:
                    out[target] = out.get(target, 0.0) + amt
                claimed.add(d.x_bas_code)
        by_name = {}
        for a in assets:                    # pass 2: name, leftovers only
            by_name.setdefault(self._norm_asset_name(a.x_bas_name_ar), []).append(a.x_bas_code)
        for d in deps:
            if d.x_bas_code in claimed:
                continue
            cands = by_name.get(self._norm_asset_name(d.x_bas_name_ar), [])
            if len(cands) == 1:
                amt = -bal(d)
                if amt:
                    out[cands[0]] = out.get(cands[0], 0.0) + amt
                claimed.add(d.x_bas_code)
        return out

    @api.model
    def action_build_asset_register(self, company=None, date_start='2026-01-01'):
        company = company or self.env.company
        Account = self.env['account.account']
        Profile = self.env['account.asset.profile']
        Asset = self.env['account.asset']
        journal = self._journal('OPEN', company)
        expense = Account.search([('x_bas_code', '=like', '33%'),
                                  ('account_type', '=', 'expense_depreciation')], limit=1)

        def balance(acc):
            self.env.cr.execute("""SELECT COALESCE(SUM(l.debit-l.credit),0)
                                   FROM account_move_line l WHERE l.account_id=%s""", (acc.id,))
            return float(self.env.cr.fetchone()[0] or 0.0)

        depmap = self._depreciation_map()
        made_p = made_a = 0
        for gcode, gname in self._ASSET_GROUPS.items():
            accs = Account.search([('x_bas_code', '=like', gcode + '%')], order='x_bas_code')
            if not accs:
                continue
            dep = Account.search([('x_bas_code', '=', '24' + gcode[2:] + '010001')], limit=1) \
                or Account.search([('x_bas_code', '=like', '24' + gcode[2:] + '%')], limit=1)
            prof = Profile.search([('name', '=', 'BAS ' + gname),
                                   ('company_id', '=', company.id)], limit=1)
            if not prof:
                prof = Profile.create({
                    'name': 'BAS ' + gname, 'company_id': company.id,
                    'journal_id': journal.id,
                    'account_asset_id': accs[0].id,
                    'account_depreciation_id': (dep or accs[0]).id,
                    'account_expense_depreciation_id': (expense or accs[0]).id,
                    # Placeholders: the register does not depreciate, because BAS
                    # holds no life or method to migrate.
                    'method': 'linear', 'method_time': 'year',
                    'method_number': 1, 'method_period': 12,
                })
                made_p += 1
            for acc in accs:
                if Asset.search_count([('code', '=', acc.x_bas_code),
                                       ('company_id', '=', company.id)]):
                    continue
                cost = balance(acc)
                if not cost:
                    continue
                asset = Asset.create({
                    'name': acc.with_context(lang='ar_001').name or acc.x_bas_code,
                    'code': acc.x_bas_code,
                    'profile_id': prof.id,
                    'purchase_value': cost,
                    'date_start': date_start,
                    'company_id': company.id,
                })
                # Accumulated depreciation to date, from the paired 24xx account,
                # recorded as an opening (init) line so net book value is right
                # without fabricating a schedule.
                accum = depmap.get(acc.x_bas_code, 0.0)
                if accum > 0:
                    self.env['account.asset.line'].create({
                        'asset_id': asset.id, 'type': 'depreciate',
                        'line_date': date_start, 'amount': accum,
                        'init_entry': True,
                        'name': 'Accumulated depreciation brought forward',
                    })
                made_a += 1
        _logger.info('BAS asset register: %s profiles, %s assets', made_p, made_a)
        return {'profiles': made_p, 'assets': made_a}

    # ------------------------------------------------------------------
    # Units of measure
    # ------------------------------------------------------------------
    # BAS writes the same unit several ways: cubic metres appear as 'cubic m'
    # (17 items), 'متر مكعب' (7) and 'Cubic meters' (1); "each" as 'حبه' (57) and
    # 'حبة' (3).  Those are spelling and language variants of one unit and are
    # unified here.
    #
    # Deliberately NOT unified: 'وحدة' (a different word, not a variant of حبة),
    # '1', and the 61 items with no unit at all.  Those are genuinely unknown --
    # mapping them would be inventing data, and they need someone who knows the
    # products.
    # Matching is case-insensitive, which also catches 'TRIP' vs 'Trip'.
    _UOM_VARIANTS = {
        'm3':    ('cubic m', 'cubic meters', 'متر مكعب'),
        'units': ('حبه', 'حبة'),
        'trip':  ('trip',),
        # Not spelling variants, but unambiguous and Odoo already has them.
        'days':  ('day', 'days'),
        'hours': ('hour', 'hours'),
    }

    @api.model
    def _uom_cubic_meter(self):
        UoM = self.env['uom.uom']
        m3 = UoM.search([('name', '=', 'm³')], limit=1)
        if not m3:
            litre = UoM.search([('name', '=', 'L')], limit=1)
            m3 = UoM.create({
                'name': 'm³',
                'relative_uom_id': litre.id if litre else False,
                'relative_factor': 1000.0 if litre else 1.0,
            })
        return m3

    @api.model
    def action_unify_units(self, company=None):
        """Point every product at one UoM per real unit, whatever BAS calls it."""
        UoM = self.env['uom.uom']
        m3 = self._uom_cubic_meter()
        units = UoM.search([('name', '=', 'Units')], limit=1)
        trip = UoM.search([('name', '=', 'Trip')], limit=1)
        if not trip:
            trip = UoM.create({'name': 'Trip', 'relative_factor': 1.0})
        target = {}
        for raw in self._UOM_VARIANTS['m3']:
            target[raw.strip().lower()] = m3
        for raw in self._UOM_VARIANTS['units']:
            target[raw.strip().lower()] = units
        for raw in self._UOM_VARIANTS['trip']:
            target[raw.strip().lower()] = trip
        for key, nm in (('days', 'Days'), ('hours', 'Hours')):
            u = UoM.search([('name', '=', nm)], limit=1)
            if u:
                for raw in self._UOM_VARIANTS[key]:
                    target[raw.strip().lower()] = u

        conn = self._bas_connect()
        cur = conn.cursor(as_dict=True)
        cur.execute("SELECT RTRIM(ICODE) code, RTRIM(ISNULL(IUNIT,'')) unit FROM ITM10")
        bas_units = {(r['code'] or '').strip(): (r['unit'] or '').strip() for r in cur.fetchall()}
        conn.close()

        changed = defaultdict(int)
        untouched = defaultdict(int)
        for p in self.env['product.product'].search([('x_bas_code', '!=', False)]):
            raw = bas_units.get(p.x_bas_code, '')
            uom = target.get(raw.strip().lower())
            if not uom:
                untouched[raw or '(none)'] += 1
                continue
            if p.uom_id != uom:
                p.product_tmpl_id.write({'uom_id': uom.id})
            changed[uom.name] += 1
        _logger.info('BAS units unified: %s; left alone: %s', dict(changed), dict(untouched))
        return {'unified': dict(changed), 'left_alone': dict(untouched)}
