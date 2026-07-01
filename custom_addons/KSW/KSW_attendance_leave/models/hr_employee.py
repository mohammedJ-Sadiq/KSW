from odoo import fields, models


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    # Upstream declares this readonly=True (never populated by the sync).
    # We override to allow HR to set it manually; _run_generate_all_absences
    # uses it to scope absence/weekend generation to the correct device.
    device_id = fields.Many2one(
        'biometric.device.details',
        string='Biometric Device',
        groups='hr.group_hr_user',
        copy=False,
        readonly=False,
    )

    x_check_in_only = fields.Boolean(
        string='Check-in Only',
        default=False,
        groups='hr.group_hr_user',
        help='If enabled, only a check-in punch is required. '
             'Early-leave deductions are suppressed; the scheduled end '
             'is used as the effective checkout for worked-hours calculation.',
    )
