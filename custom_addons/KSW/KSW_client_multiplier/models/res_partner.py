from odoo import api, fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # Shown on the customer (Sales & Purchase), read-only: the value is
    # governed by requests and the GM, its home is Water Delivery ▸ Clients.
    x_multiplier = fields.Float(
        string='Multiplier (الرد المضاعف)', compute='_compute_x_multiplier',
        compute_sudo=True, digits=(16, 2),
        help='The customer\'s multiplier in force today. It changes only through a '
             'Multiplier Request approved by the GM.')
    x_multiplier_client_id = fields.Many2one(
        'ksw.client.multiplier', compute='_compute_x_multiplier', compute_sudo=True)

    def _compute_x_multiplier(self):
        clients = self.env['ksw.client.multiplier'].sudo().search(
            [('partner_id', 'in', self.commercial_partner_id.ids)])
        by_partner = {c.partner_id.id: c for c in clients}
        for partner in self:
            client = by_partner.get(partner.commercial_partner_id.id)
            partner.x_multiplier = client.current_multiplier if client else 0.0
            partner.x_multiplier_client_id = client

    def action_open_multiplier(self):
        self.ensure_one()
        client = self.x_multiplier_client_id
        if not client:
            return False
        return {
            'type': 'ir.actions.act_window', 'res_model': 'ksw.client.multiplier',
            'res_id': client.id, 'view_mode': 'form', 'target': 'current',
        }

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
