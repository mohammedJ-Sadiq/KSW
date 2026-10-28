from odoo import fields, models


class KswStockMovementLine(models.TransientModel):
    _inherit = 'ksw.stock.movement.line'

    x_multiplier = fields.Float(
        string='Client Multiplier', readonly=True, aggregator='sum', digits=(16, 2),
        help='«الرد المضاعف»: the customer\'s multiplier in force on the day of '
             'this movement. Summed in a group, it is the weighted load count. '
             'Empty when the row has no customer or the customer has none.')


class KswStockMovementWizard(models.TransientModel):
    _inherit = 'ksw.stock.movement.wizard'

    def _line_vals(self):
        vals_list = super()._line_vals()
        Partner = self.env['res.partner']
        commercial = {}
        for vals in vals_list:
            if vals.get('partner_id') and vals.get('kind') != 'opening':
                commercial.setdefault(vals['partner_id'], None)
        if not commercial:
            return vals_list
        for partner in Partner.browse(list(commercial)).sudo():
            commercial[partner.id] = partner.commercial_partner_id.id
        clients = self.env['ksw.client.multiplier'].sudo().search(
            [('partner_id', 'in', list(set(commercial.values())))])
        client_of = {c.partner_id.id: c for c in clients}
        cache = {}
        for vals in vals_list:
            client = client_of.get(commercial.get(vals.get('partner_id')))
            if not client or vals.get('kind') == 'opening' or not vals.get('date'):
                continue
            key = (client.id, vals['date'])
            if key not in cache:
                rate = client._rate_on(vals['date'])
                cache[key] = rate.multiplier if rate else 0.0
            vals['x_multiplier'] = cache[key]
        return vals_list
