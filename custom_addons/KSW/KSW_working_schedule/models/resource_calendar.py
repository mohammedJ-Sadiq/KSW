from odoo import fields, models


class ResourceCalendar(models.Model):
    _inherit = 'resource.calendar'

    is_temp_schedule = fields.Boolean(
        string='Is temporary Schedule',
        help='Indicates if this calendar is a temporary work schedule for employees',
    )

    x_saturday_required = fields.Boolean(
        string='Saturday Required (Deduct + Overtime Offset)',
        help='When set, Saturday is treated as a required workday for '
             'attendance-sheet purposes even if it has no schedule line in '
             'this calendar. Absences are deducted normally and an equal '
             'amount is credited back as Saturday overtime on the payslip.',
    )

    calendar_group_ids = fields.Many2many(
        'resource.calendar.group',
        'resource_calendar_group_rel',
        'calendar_id',
        'group_id',
        string='Calendar Groups',
    )
