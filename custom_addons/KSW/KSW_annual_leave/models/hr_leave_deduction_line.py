from odoo import fields, models


class HrLeaveDeductionLine(models.Model):
    _name = 'hr.leave.deduction.line'
    _description = 'Leave Other Deduction Line'
    _order = 'sequence, id'

    leave_id = fields.Many2one(
        'hr.leave', string='Leave', required=True, ondelete='cascade',
        index=True,
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(
        string='Description', required=True,
        help='E.g. "Uniform cost", "Traffic fine Jan 2026", etc.',
    )
    amount = fields.Float(
        string='Amount', digits=(16, 2), required=True,
        help='Positive amount. It is deducted from the vacation payslip.',
    )
