from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ItAssetAssignWizard(models.TransientModel):
    _name = 'it.asset.assign.wizard'
    _description = 'Assign IT Asset'

    asset_id = fields.Many2one('it.asset', required=True, readonly=True)
    employee_id = fields.Many2one('hr.employee', required=True)
    date_assigned = fields.Date(default=fields.Date.context_today, required=True)
    condition_out = fields.Selection([
        ('new', 'New'),
        ('good', 'Good'),
        ('fair', 'Fair'),
        ('poor', 'Poor'),
    ], string='Condition', default='good')
    notes = fields.Text()

    def action_confirm(self):
        self.ensure_one()
        if self.asset_id.state != 'available':
            raise UserError(_('Only an available asset can be assigned.'))
        self.env['it.asset.assignment'].create({
            'asset_id': self.asset_id.id,
            'employee_id': self.employee_id.id,
            'date_assigned': self.date_assigned,
            'condition_out': self.condition_out,
            'notes': self.notes,
        })
        self.asset_id.write({
            'state': 'assigned',
            'employee_id': self.employee_id.id,
        })
        return {'type': 'ir.actions.act_window_close'}
