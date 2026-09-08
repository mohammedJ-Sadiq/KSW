from odoo import fields, models


class ItAssetMaintenance(models.Model):
    _name = 'it.asset.maintenance'
    _description = 'IT Asset Maintenance'
    _order = 'date_start desc, id desc'

    asset_id = fields.Many2one('it.asset', required=True, ondelete='cascade', index=True)
    category_id = fields.Many2one(related='asset_id.category_id', store=True, readonly=True)
    issue = fields.Char(required=True)
    vendor_id = fields.Many2one('res.partner', string='Repaired By')
    date_start = fields.Date(default=fields.Date.context_today, required=True)
    date_end = fields.Date()
    currency_id = fields.Many2one(related='asset_id.currency_id')
    cost = fields.Monetary(currency_field='currency_id')
    state = fields.Selection([
        ('in_progress', 'In Progress'),
        ('done', 'Done'),
    ], default='in_progress', required=True)
    notes = fields.Text()
    company_id = fields.Many2one(related='asset_id.company_id', store=True, readonly=True)
