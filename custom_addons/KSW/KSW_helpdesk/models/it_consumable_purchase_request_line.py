from odoo import api, fields, models


class ItConsumablePurchaseRequestLine(models.Model):
    _name = 'it.consumable.purchase.request.line'
    _description = 'IT Consumable Purchase Request Line'

    request_id = fields.Many2one(
        'it.consumable.purchase.request', required=True, ondelete='cascade', index=True,
    )
    consumable_id = fields.Many2one('it.consumable', required=True)
    category_id = fields.Many2one(related='consumable_id.category_id', store=True, readonly=True)
    uom = fields.Selection(related='consumable_id.uom', readonly=True)
    # Snapshot of the stock level when the line was added - deliberately not
    # related=, so the printed request still reflects what stock looked
    # like at request time even if it has since moved.
    qty_on_hand = fields.Integer(string='Current Stock')
    quantity = fields.Integer(string='Quantity to Order', required=True, default=1)
    currency_id = fields.Many2one(related='request_id.currency_id')
    unit_cost = fields.Monetary(currency_field='currency_id')
    subtotal = fields.Monetary(
        compute='_compute_subtotal', store=True, currency_field='currency_id',
    )

    @api.depends('quantity', 'unit_cost')
    def _compute_subtotal(self):
        for line in self:
            line.subtotal = line.quantity * line.unit_cost

    @api.onchange('consumable_id')
    def _onchange_consumable_id(self):
        for line in self:
            if line.consumable_id:
                line.qty_on_hand = line.consumable_id.qty_on_hand
                line.quantity = line.consumable_id.reorder_qty or 1
                line.unit_cost = line.consumable_id.unit_cost
