from odoo import fields, models


class KswSalesCommissionDeductionLine(models.Model):
    _name = 'ksw.sales.commission.deduction.line'
    _description = 'Sales/Collection Commission — Deduction Line'
    _order = 'sequence, id'

    line_id = fields.Many2one(
        'ksw.sales.commission.line', string='Commission Line',
        required=True, ondelete='cascade', index=True,
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(
        string='Description', required=True,
        help='E.g. "Traffic fine Jan 2026", "Shortfall correction".',
    )
    amount = fields.Monetary(
        string='Amount', required=True,
        help='Positive amount. Deducted from this employee\'s total commission.',
    )
    currency_id = fields.Many2one(
        related='line_id.currency_id', store=True, readonly=True,
    )
