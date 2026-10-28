from odoo import api, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # A customer created in Odoo has no BAS account, so the daily BAS refresh
    # never gives it a «الرد المضاعف». It starts at 1 the moment it becomes a
    # customer: created as one (a customer picker stamps customer_rank), or on
    # its first sale (account's _increase_rank writes 0 -> 1 through the ORM).
    @api.model_create_multi
    def create(self, vals_list):
        partners = super().create(vals_list)
        customers = partners.filtered(lambda p: p.customer_rank > 0 and not p.parent_id)
        if customers:
            self.env['ksw.client.multiplier']._ensure_for_partners(customers)
        return partners

    def write(self, vals):
        newly_customers = self.browse()
        if vals.get('customer_rank'):
            newly_customers = self.filtered(lambda p: not p.customer_rank)
        res = super().write(vals)
        if newly_customers:
            self.env['ksw.client.multiplier']._ensure_for_partners(newly_customers)
        return res
