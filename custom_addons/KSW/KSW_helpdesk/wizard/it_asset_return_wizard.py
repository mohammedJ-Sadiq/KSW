from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ItAssetReturnWizard(models.TransientModel):
    _name = 'it.asset.return.wizard'
    _description = 'Return IT Asset'

    asset_id = fields.Many2one('it.asset', required=True, readonly=True)
    date_returned = fields.Date(default=fields.Date.context_today, required=True)
    condition_in = fields.Selection([
        ('new', 'New'),
        ('good', 'Good'),
        ('fair', 'Fair'),
        ('poor', 'Poor'),
    ], string='Condition', default='good')
    notes = fields.Text()

    def action_confirm(self):
        self.ensure_one()
        asset = self.asset_id
        if asset.state != 'assigned':
            raise UserError(_('This asset is not currently assigned.'))
        open_assignment = self.env['it.asset.assignment'].search([
            ('asset_id', '=', asset.id), ('state', '=', 'active'),
        ], limit=1, order='date_assigned desc')
        if open_assignment:
            open_assignment.write({
                'state': 'returned',
                'date_returned': self.date_returned,
                'condition_in': self.condition_in,
                'notes': (open_assignment.notes or '') + (('\n' + self.notes) if self.notes else ''),
            })
        asset.write({'state': 'available', 'employee_id': False})
        return {'type': 'ir.actions.act_window_close'}
