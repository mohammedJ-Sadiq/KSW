from odoo import api, models

# BAS keeps every company bank account as a leaf under 1202 (cash type).  The
# only bank journal Odoo created is the chart template's BNK1 on 101001 "Bank",
# an account BAS does not have, so every payment landed there.
BAS_BANK_PREFIX = '1202'
TEMPLATE_BANK_CODE = '101001'


class AccountJournal(models.Model):
    _inherit = 'account.journal'

    @api.model
    def _ksw_ensure_bas_bank_journals(self):
        """One bank journal per BAS bank account; retire the template BNK1.

        Idempotent: runs on every upgrade.  A no-op on a database without the
        BAS chart (no 1202 accounts), so it is safe to ship anywhere.
        """
        Account = self.env['account.account'].sudo()
        for company in self.env['res.company'].sudo().search([]):
            banks = Account.with_company(company).search([
                *Account._check_company_domain(company),
                ('code', '=like', BAS_BANK_PREFIX + '%'),
                ('account_type', '=', 'asset_cash'),
            ])
            if not banks:
                continue
            journals = self.sudo().with_company(company).with_context(active_test=False)
            for acc in banks:
                journal = journals.search([
                    ('company_id', '=', company.id), ('type', '=', 'bank'),
                    ('default_account_id', '=', acc.id),
                ], limit=1)
                if not journal:
                    journal = journals.create({
                        'name': acc.with_context(lang='ar_001').name,
                        'code': 'B' + acc.code[-4:],
                        'type': 'bank',
                        'company_id': company.id,
                        'default_account_id': acc.id,
                    })
                # Post straight to the bank, as BAS does.  Leaving the payment
                # account empty does not do that: Odoo falls back to 101003
                # Outstanding Receipts (same trap as the BAS tills).
                (journal.inbound_payment_method_line_ids
                 | journal.outbound_payment_method_line_ids).payment_account_id = acc
            journals.search([
                ('company_id', '=', company.id), ('type', '=', 'bank'),
                ('default_account_id.code', '=', TEMPLATE_BANK_CODE),
                ('active', '=', True),
            ]).active = False
