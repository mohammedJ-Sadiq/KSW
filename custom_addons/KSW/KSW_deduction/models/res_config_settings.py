from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # Lives here rather than in KSW_payroll: the payslip-revision feature
    # is in KSW_payroll, but `ksw.deduction.type` is defined by this module,
    # which depends on it — a Many2one cannot point the other way.
    ksw_overpay_recovery_type_id = fields.Many2one(
        'ksw.deduction.type',
        string='Over-payment Recovery Type',
        config_parameter='ksw_payroll.overpay_recovery_type_id',
        default=lambda self: self.env.ref(
            'KSW_deduction.type_advance', raise_if_not_found=False),
        help='Deduction type used when a payslip revision shows the '
             'employee was over-paid. Confirming such a revision cancels '
             'it and opens a draft deduction of this type for the '
             'difference, so the amount is recovered from a later payroll '
             'run instead of a negative net being paid.',
    )
