from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ItConsumableMove(models.Model):
    _name = 'it.consumable.move'
    _description = 'IT Consumable Stock Move'
    _order = 'date desc, id desc'

    consumable_id = fields.Many2one('it.consumable', required=True, ondelete='cascade', index=True)
    category_id = fields.Many2one(related='consumable_id.category_id', store=True, readonly=True)
    move_type = fields.Selection([
        ('in', 'Receive'),
        ('out', 'Issue'),
        ('adjustment', 'Stock Count Adjustment'),
    ], required=True, default='out')
    quantity = fields.Integer(required=True, default=1)
    date = fields.Datetime(default=fields.Datetime.now, required=True)
    # Only meaningful for 'out' moves - who took the stock.
    employee_id = fields.Many2one('hr.employee', string='Issued To')
    # Optional link to the asset (e.g. printer) the item was used on/for.
    asset_id = fields.Many2one('it.asset', string='Used For')
    supplier_id = fields.Many2one('res.partner', string='Received From')
    done_by = fields.Many2one('res.users', default=lambda self: self.env.user, readonly=True)
    note = fields.Char()
    company_id = fields.Many2one(related='consumable_id.company_id', store=True, readonly=True)

    @api.constrains('quantity')
    def _check_quantity_positive(self):
        for move in self:
            if move.quantity <= 0:
                raise ValidationError(_('The quantity of a stock move must be greater than zero.'))
