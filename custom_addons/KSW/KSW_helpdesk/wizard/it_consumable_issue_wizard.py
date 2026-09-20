from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ItConsumableIssueWizard(models.TransientModel):
    _name = 'it.consumable.issue.wizard'
    _description = 'Issue IT Consumable'

    consumable_id = fields.Many2one('it.consumable', required=True, readonly=True)
    qty_on_hand = fields.Integer(related='consumable_id.qty_on_hand')
    quantity = fields.Integer(required=True, default=1)
    employee_id = fields.Many2one('hr.employee', string='Issued To', required=True)
    asset_id = fields.Many2one(
        'it.asset', string='Used For',
        domain="[('employee_id', '=', employee_id)]",
        help="The asset (e.g. printer) this item was used on, if any. "
             "Only assets currently assigned to the selected employee are shown.",
    )
    note = fields.Char()

    @api.constrains('quantity')
    def _check_quantity_positive(self):
        for wizard in self:
            if wizard.quantity <= 0:
                raise UserError(_('The quantity must be greater than zero.'))

    @api.onchange('employee_id')
    def _onchange_employee_id(self):
        if self.asset_id and self.asset_id.employee_id != self.employee_id:
            self.asset_id = False

    def action_confirm(self):
        self.ensure_one()
        if self.quantity > self.consumable_id.qty_on_hand:
            raise UserError(_(
                'Only %(available)d %(item)s in stock - cannot issue %(requested)d.',
                available=self.consumable_id.qty_on_hand,
                item=self.consumable_id.name,
                requested=self.quantity,
            ))
        self.env['it.consumable.move'].create({
            'consumable_id': self.consumable_id.id,
            'move_type': 'out',
            'quantity': self.quantity,
            'employee_id': self.employee_id.id,
            'asset_id': self.asset_id.id,
            'note': self.note,
        })
        return {'type': 'ir.actions.act_window_close'}
