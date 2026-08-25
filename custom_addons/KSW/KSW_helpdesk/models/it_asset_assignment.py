from odoo import fields, models


class ItAssetAssignment(models.Model):
    _name = 'it.asset.assignment'
    _description = 'IT Asset Assignment'
    _order = 'date_assigned desc, id desc'

    asset_id = fields.Many2one('it.asset', required=True, ondelete='cascade', index=True)
    category_id = fields.Many2one(related='asset_id.category_id', store=True, readonly=True)
    employee_id = fields.Many2one('hr.employee', required=True, index=True)
    assigned_by = fields.Many2one('res.users', default=lambda self: self.env.user, readonly=True)
    date_assigned = fields.Date(default=fields.Date.context_today, required=True)
    date_returned = fields.Date()
    state = fields.Selection([
        ('active', 'Active'),
        ('returned', 'Returned'),
    ], default='active', required=True)
    condition_out = fields.Selection([
        ('new', 'New'),
        ('good', 'Good'),
        ('fair', 'Fair'),
        ('poor', 'Poor'),
    ], string='Condition (Out)', default='good')
    condition_in = fields.Selection([
        ('new', 'New'),
        ('good', 'Good'),
        ('fair', 'Fair'),
        ('poor', 'Poor'),
    ], string='Condition (Returned)')
    notes = fields.Text()
    company_id = fields.Many2one(related='asset_id.company_id', store=True, readonly=True)
