from odoo import _, api, models
from odoo.exceptions import UserError

# BAS gives every client its own leaf under a customer group (AD41 is
# 120301025); there is no shared "Accounts Receivable".  The l10n_sa default
# (102011) is retired, so a partner gets its own leaf the first time it is used
# as a customer.  The group is a system parameter so accounting can move it.
PREFIX_PARAM = 'KSW_accounting_ux.customer_receivable_prefix'
DEFAULT_PREFIX = '120301'


class ResPartner(models.Model):
    _inherit = 'res.partner'

    def _ksw_ensure_receivable_account(self, company=None):
        company = company or self.env.company
        missing = self.commercial_partner_id.sudo().with_company(company).filtered(
            lambda p: not p.property_account_receivable_id.active)
        if not missing:
            return
        prefix = self.env['ir.config_parameter'].sudo().get_param(PREFIX_PARAM, DEFAULT_PREFIX)
        Account = self.env['account.account'].sudo().with_company(company).with_context(active_test=False)
        siblings = Account.search([
            *Account._check_company_domain(company),
            ('code', '=like', prefix + '___'),
        ], order='code desc')
        if not siblings:
            return  # No BAS chart here: core's default receivable still applies.
        for partner in missing:
            last = max(int(c) for c in siblings.mapped('code') if c.isdigit())
            code = str(last + 1)
            if not code.startswith(prefix):
                raise UserError(_('No free receivable code left under %s.', prefix))
            account = Account.create({
                'code': code,
                'name': partner.name,
                'account_type': 'asset_receivable',
                'reconcile': True,
                'company_ids': [(4, company.id)],
            })
            partner.property_account_receivable_id = account
            siblings |= account

    @api.model_create_multi
    def create(self, vals_list):
        partners = super().create(vals_list)
        partners.filtered(lambda p: p.customer_rank > 0)._ksw_ensure_receivable_account()
        return partners

    def write(self, vals):
        res = super().write(vals)
        if vals.get('customer_rank'):
            self._ksw_ensure_receivable_account()
        return res
