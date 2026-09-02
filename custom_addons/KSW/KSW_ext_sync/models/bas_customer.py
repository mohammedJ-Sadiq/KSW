import logging
from odoo import models, fields, api

_logger = logging.getLogger(__name__)

# COD10 leaf accounts under 1203* (DACC_TYPE='01', DLEVEL=5) are BAS's real
# trade-customer ledger. Verified live 2026-08-11: DACC_TYPE='10' (which the
# reference doc labels "Customer accounts") is actually depreciation
# accounts, and 1201* (labelled "مديني المبيعات / customer accounts" in the
# doc) is actually internal cash/treasury accounts. 1203* leaf rows were
# spot-checked (25 random rows) and are genuinely trade customers (companies,
# shops, institutions) — the doc has been corrected accordingly.


class BASCustomer(models.Model):
    _name = 'ksw.bas.customer'
    _description = 'BAS Customer Account'
    _inherit = ['ksw.bas.connector']
    _order = 'bas_code'

    bas_code = fields.Char('Account Code', readonly=True, index=True)
    name_ar = fields.Char('Name (Arabic)', readonly=True)
    name_en = fields.Char('Name (English)', readonly=True)
    is_stopped = fields.Boolean('Stopped in BAS', readonly=True)
    seller_code = fields.Char(
        'Sales Rep Code', readonly=True,
        help='COD10.SELLER — the sales rep BAS itself assigns to this '
             'customer. Verified 2026-08-11 against real sales transactions '
             '(the transaction-level vou10.SELLER on this customer\'s '
             'invoices always matches this code).',
    )
    seller_name = fields.Char(
        'Sales Rep Name (BAS)', readonly=True,
        help='Resolved from VOU10.SELLERNAME for this SELLER code. Matched '
             'to an hr.employee via x_commission_import_name (or name) by '
             '"Pull from BAS" on the Sales/Collection Commission Sheet.',
    )
    collector_code = fields.Char(
        'Collection Rep Code', readonly=True,
        help='COD10.SELLER2 — the collection rep BAS itself assigns to '
             'this customer. Verified 2026-08-11 against real receipt '
             'transactions.',
    )
    collector_name = fields.Char('Collection Rep Name (BAS)', readonly=True)
    credit_term_days = fields.Integer(
        'Credit Term (Days)', readonly=True,
        help='COD10.LIMT_DAYS — the payment term BAS itself assigns to '
             'this customer (e.g. 30/60/90). Matches this project\'s '
             '"عمر الدين" (debt age) column. Used by KSW_commissions to '
             'decide how much of the outstanding balance counts as '
             'overdue for the BAS-derived collection target. Falls back '
             'to 30 days when unset/zero.',
    )
    last_synced = fields.Datetime('Last Synced', readonly=True)
    partner_id = fields.Many2one(
        'res.partner', string='Odoo Contact', ondelete='set null',
        help='Contact linked to this BAS customer account, either matched '
             'by name to an existing contact or created automatically by '
             '"Match / Create Contacts".',
    )
    partner_created = fields.Boolean(
        readonly=True,
        help='True when the linked contact was created by "Match / Create '
             'Contacts" rather than matched to a pre-existing one.',
    )

    # Everything but ``last_synced`` and ``partner_id`` — the latter is
    # linked by its own pass below, not by comparison.
    _COMPARE_FIELDS = (
        'name_ar', 'name_en', 'is_stopped', 'seller_code', 'seller_name',
        'collector_code', 'collector_name', 'credit_term_days',
    )

    @api.model
    def sync_from_bas(self, deadline=None, commit=True):
        try:
            conn = self._bas_connect()
            cursor = conn.cursor(as_dict=True)
        except Exception as e:
            _logger.error('KSW BAS customer sync: connection failed: %s', e)
            return False

        try:
            cursor.execute("""
                SELECT DCODE1, DNAME, DNAME2, ISNULL(DSTOP, 0) AS DSTOP,
                       SELLER, SELLER2, ISNULL(LIMT_DAYS, 0) AS LIMT_DAYS
                FROM COD10
                WHERE DACC_TYPE = '01' AND DLEVEL = 5
                  AND DCODE1 LIKE '1203%'
                ORDER BY DCODE1
            """)
            rows = cursor.fetchall()
            # SELLER is only a numeric code on COD10 — VOU10.SELLERNAME is
            # the only place BAS stores the human name for that code
            # (no dedicated salesman master table exists). Some codes have
            # both an Arabic and a Latin-script name recorded over time;
            # prefer the Latin one to match this project's existing
            # x_commission_import_name convention (e.g. "Abu Sadeq").
            cursor.execute("""
                SELECT DISTINCT SELLER, SELLERNAME FROM vou10
                WHERE SELLER IS NOT NULL AND SELLERNAME IS NOT NULL
                  AND SELLERNAME <> ''
            """)
            seller_names = {}
            for r in cursor.fetchall():
                key = (r['SELLER'] or '').strip()
                name = (r['SELLERNAME'] or '').strip()
                if not key or not name:
                    continue
                if key not in seller_names or (
                        name.isascii() and not seller_names[key].isascii()):
                    seller_names[key] = name
        except Exception as e:
            _logger.error('KSW BAS customer sync: query failed: %s', e)
            conn.close()
            return False
        finally:
            conn.close()

        def _seller_code(value):
            if value in (None, '', -1, '-1'):
                return ''
            try:
                return str(int(value))
            except (TypeError, ValueError):
                return str(value).strip()

        now = fields.Datetime.now()

        vals_list = []
        for row in rows:
            code = (row['DCODE1'] or '').strip()
            if not code:
                continue
            seller_code = _seller_code(row['SELLER'])
            collector_code = _seller_code(row['SELLER2'])
            vals_list.append({
                'bas_code': code,
                'name_ar': (row['DNAME'] or '').strip(),
                'name_en': (row['DNAME2'] or '').strip(),
                'is_stopped': bool(row['DSTOP']),
                'seller_code': seller_code,
                'seller_name': seller_names.get(seller_code, ''),
                'collector_code': collector_code,
                'collector_name': seller_names.get(collector_code, ''),
                'credit_term_days': int(row['LIMT_DAYS'] or 0),
                'last_synced': now,
            })

        created, updated, done = self._bas_upsert(
            'bas_code', vals_list, self._COMPARE_FIELDS,
            deadline=deadline, commit=commit)

        # Contacts that already carry the account number get linked for
        # free — safe, since it's an existing explicit assignment.  Kept a
        # separate pass so `partner_id` never enters the change comparison:
        # it is ours, not BAS's, and must never be overwritten from there.
        partners_by_code = {
            (p.x_client_account_number or '').strip(): p
            for p in self.env['res.partner'].search(
                [('x_client_account_number', '!=', False)])
        }
        if partners_by_code:
            unlinked = self.search([
                ('partner_id', '=', False),
                ('bas_code', 'in', list(partners_by_code)),
            ])
            for rec in unlinked:
                rec.partner_id = partners_by_code[rec.bas_code].id

        if done:
            # Only prune on a complete pass (see ksw.bas.account).
            synced_codes = [v['bas_code'] for v in vals_list]
            stale = self.search([('bas_code', 'not in', synced_codes)])
            if stale:
                stale.unlink()

        _logger.info(
            'KSW BAS: customers %s — %d read, %d created, %d updated',
            'complete' if done else 'INCOMPLETE (time budget)',
            len(rows), created, updated)
        return done

    # ------------------------------------------------------------------
    # Manual, explicit bulk match/create — not run by the 10-min cron.
    # Creating contacts is a bigger, less reversible action than syncing
    # reference rows, so it stays a deliberate button click.
    # ------------------------------------------------------------------
    @api.model
    def action_match_or_create_partners(self, *args, **kwargs):
        # The list-view header button passes the currently displayed/
        # selected record ids as a positional arg even for an @api.model
        # method — accept and ignore it; this always processes every
        # unlinked, active ksw.bas.customer regardless of selection.
        Partner = self.env['res.partner']
        todo = self.search([
            ('partner_id', '=', False), ('is_stopped', '=', False),
        ])
        if not todo:
            return {
                'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {
                    'title': 'BAS Customers',
                    'message': 'Nothing to match — every active BAS '
                               'customer already has a linked contact.',
                    'sticky': False,
                    # display_notification alone doesn't refresh the list
                    # (it's a client action, not a view reload) — chain a
                    # soft_reload so newly linked/created contacts show up
                    # without a manual browser refresh.
                    'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
                },
            }

        candidates = Partner.search([])
        by_name = {}
        for p in candidates:
            for key in (p.name, p.x_commission_import_name):
                norm = ' '.join((key or '').split()).strip().lower()
                if norm and norm not in by_name:
                    by_name[norm] = p

        matched = created = 0
        for rec in todo:
            norm_en = ' '.join((rec.name_en or '').split()).strip().lower()
            norm_ar = ' '.join((rec.name_ar or '').split()).strip().lower()
            partner = by_name.get(norm_en) or by_name.get(norm_ar)
            if partner and not partner.x_client_account_number:
                rec.write({'partner_id': partner.id, 'partner_created': False})
                partner.write({'x_client_account_number': rec.bas_code})
                matched += 1
            else:
                new_partner = Partner.create({
                    'name': rec.name_en or rec.name_ar or rec.bas_code,
                    'is_company': True,
                    'customer_rank': 1,
                    'x_client_account_number': rec.bas_code,
                })
                rec.write({
                    'partner_id': new_partner.id, 'partner_created': True,
                })
                created += 1

        _logger.info(
            'KSW BAS: customer match/create — %d matched, %d created',
            matched, created)
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'title': 'BAS Customers',
                'message': f'{matched} contact(s) matched, '
                           f'{created} new contact(s) created.',
                'sticky': True,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }
