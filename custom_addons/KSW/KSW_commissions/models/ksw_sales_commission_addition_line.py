from odoo import fields, models


class KswSalesCommissionAdditionLine(models.Model):
    _name = 'ksw.sales.commission.addition.line'
    _description = 'Sales/Collection Commission — Addition Line'
    _order = 'sequence, id'

    line_id = fields.Many2one(
        'ksw.sales.commission.line', string='Commission Line',
        required=True, ondelete='cascade', index=True,
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(
        string='Description', required=True,
        help='E.g. "Reward for closing the ACME deal", "Feb 2026 bonus".',
    )
    amount = fields.Monetary(
        string='Amount', required=True,
        help='Positive amount. Added to this employee\'s total commission.',
    )
    currency_id = fields.Many2one(
        related='line_id.currency_id', store=True, readonly=True,
    )
