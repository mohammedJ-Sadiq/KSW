from odoo import api, models


class HrLeave(models.Model):
    _inherit = 'hr.leave'

    @api.depends('date_from', 'date_to', 'resource_calendar_id',
                 'holiday_status_id.request_unit', 'holiday_status_id.is_sick_leave')
    def _compute_duration(self):
        # Split: sick leave uses calendar days; everything else follows the
        # standard work-schedule computation.
        sick = self.filtered(
            lambda l: l.holiday_status_id and l.holiday_status_id.is_sick_leave
                      and l.request_date_from and l.request_date_to
        )
        rest = self - sick

        if rest:
            super(HrLeave, rest)._compute_duration()

        for leave in sick:
            cal_days = (leave.request_date_to - leave.request_date_from).days + 1
            leave.number_of_days = cal_days
            # Keep hours proportional (8 h/day default) so downstream code
            # that reads number_of_hours does not see 0.
            leave.number_of_hours = cal_days * (
                leave.resource_calendar_id.hours_per_day or 8.0
            )
