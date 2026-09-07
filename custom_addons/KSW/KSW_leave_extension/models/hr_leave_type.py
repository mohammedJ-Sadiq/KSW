from odoo import fields, models


class HrLeaveType(models.Model):
    _inherit = 'hr.leave.type'

    is_leave_extension = fields.Boolean(
        string='Is Vacation Extension',
        default=False,
        help='Check this box if this leave type extends an already approved '
             'vacation. Requests of this type must name the vacation they '
             'continue and start the day after it ends.',
    )

    leave_validation_type = fields.Selection(
        selection_add=[
            ('extension_multi', 'Vacation Extension – Multi-Step Approval'),
        ],
        ondelete={'extension_multi': 'set default'},
    )
