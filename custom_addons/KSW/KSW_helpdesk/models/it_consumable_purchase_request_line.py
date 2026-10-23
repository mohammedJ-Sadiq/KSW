from odoo import api, fields, models


class ItConsumablePurchaseRequestLine(models.Model):
    _name = 'it.consumable.purchase.request.line'
    _description = 'IT Consumable Purchase Request Line'

    request_id = fields.Many2one(
        'it.consumable.purchase.request', required=True, ondelete='cascade', index=True,
    )
    # Optional: leave empty for a one-off purchase that is not a stocked
    # consumable (e.g. a new laptop) - only the Description is then used,
    # and Mark as Purchased creates no stock move for the line.
    consumable_id = fields.Many2one(
        'it.consumable', string='Consumable',
        help="Pick a stocked consumable to restock it, or leave empty for a "
             "one-off item outside the consumables list (e.g. a new laptop).",
    )
    name = fields.Char(
        string='Description', required=True,
        compute='_compute_name', store=True, readonly=False, precompute=True,
    )
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

    @api.depends('consumable_id')
    def _compute_name(self):
        for line in self:
            if line.consumable_id:
                line.name = line.consumable_id.name

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
