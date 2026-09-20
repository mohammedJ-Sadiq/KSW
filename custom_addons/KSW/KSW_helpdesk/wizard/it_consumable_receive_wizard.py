from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ItConsumableReceiveWizard(models.TransientModel):
    _name = 'it.consumable.receive.wizard'
    _description = 'Receive IT Consumable Stock'

    consumable_id = fields.Many2one('it.consumable', required=True, readonly=True)
    quantity = fields.Integer(required=True, default=1)
    supplier_id = fields.Many2one('res.partner', string='Received From')
    unit_cost = fields.Monetary(currency_field='currency_id')
    currency_id = fields.Many2one(related='consumable_id.currency_id')
    update_unit_cost = fields.Boolean(
        string='Update Unit Cost',
        help="Also update the item's standard unit cost to the value above.",
    )
    note = fields.Char()

    @api.constrains('quantity')
    def _check_quantity_positive(self):
        for wizard in self:
            if wizard.quantity <= 0:
                raise UserError(_('The quantity must be greater than zero.'))

    def action_confirm(self):
        self.ensure_one()
        self.env['it.consumable.move'].create({
            'consumable_id': self.consumable_id.id,
            'move_type': 'in',
            'quantity': self.quantity,
            'supplier_id': self.supplier_id.id,
            'note': self.note,
        })
        if self.update_unit_cost and self.unit_cost:
            self.consumable_id.unit_cost = self.unit_cost
        return {'type': 'ir.actions.act_window_close'}
