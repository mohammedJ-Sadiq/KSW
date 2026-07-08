from odoo import fields, models


class HrLeaveType(models.Model):
    _inherit = 'hr.leave.type'

    is_eos_leave = fields.Boolean(
        string='Is EOS Leave',
        default=False,
        help='Mark this leave type as an End of Service request. '
             'Enables EOS-specific fields and payslip generation during the '
             'multi-step approval chain.',
    )
