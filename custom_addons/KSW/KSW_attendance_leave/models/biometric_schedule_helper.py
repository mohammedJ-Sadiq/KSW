import pytz

from odoo import api, models

# Night window used to classify a calendar as night-shift: 20:00-24:00 and
# 00:00-10:00. Hours falling outside this window count as "day" hours.
_NIGHT_WINDOW_START = 20.0
_NIGHT_WINDOW_END = 10.0
_NIGHT_SHIFT_RATIO_THRESHOLD = 0.5


def _shift_duration(hour_from, hour_to):
    """Length in hours of [hour_from, hour_to), wrapping past midnight."""
    return (hour_to - hour_from) if hour_to > hour_from else (24.0 - hour_from + hour_to)


def _night_hours_in_segment(seg_from, seg_to):
    """Hours of [seg_from, seg_to) (no wraparound) inside the night window."""
    overlap_late = max(0.0, min(seg_to, 24.0) - max(seg_from, _NIGHT_WINDOW_START))
    overlap_early = max(0.0, min(seg_to, _NIGHT_WINDOW_END) - max(seg_from, 0.0))
    return overlap_late + overlap_early


def _night_hours(hour_from, hour_to):
    """Hours of [hour_from, hour_to) inside the night window, wrapping past midnight."""
    if hour_to <= hour_from:
        return _night_hours_in_segment(hour_from, 24.0) + _night_hours_in_segment(0.0, hour_to)
    return _night_hours_in_segment(hour_from, hour_to)


class BiometricScheduleHelperKSW(models.AbstractModel):
    _inherit = 'biometric.schedule.helper'

    @api.model
    def detect_night_shift(self, employee):
        """Hours-weighted override of the base line-count heuristic.

        The base implementation only counts a line as "night" if it
        literally wraps midnight in one record (hour_from > hour_to). A
        shift entered as two same-day lines (e.g. 21:00-1:00 stored
        alongside its 1:00-5:00 continuation, as our shift calendars do)
        then never has more than half its lines flagged, so it can never
        clear the base method's 70% line-count bar and is never detected
        as night shift -- which is what broke date-bucketing for
        biometric punches on overnight shifts. Weighting by scheduled
        hours instead of line count fixes this regardless of how the
        shift's lines are split.
        """
        calendar = employee.resource_calendar_id or employee.company_id.resource_calendar_id
        if not calendar:
            return False
        schedules = self.env['resource.calendar.attendance'].search([
            ('calendar_id', '=', calendar.id),
            ('day_period', '!=', 'lunch'),
        ])
        if not schedules:
            return False

        total_hours = sum(_shift_duration(s.hour_from, s.hour_to) for s in schedules)
        if total_hours <= 0:
            return False
        night_hours = sum(_night_hours(s.hour_from, s.hour_to) for s in schedules)

        return (night_hours / total_hours) >= _NIGHT_SHIFT_RATIO_THRESHOLD

    @api.model
    def calculate_worked_time(self, check_in, check_out, employee):
        """For check-in-only employees, replace checkout with the scheduled
        end time so that no early-leave penalty is ever computed."""
        if employee and employee.x_check_in_only:
            emp_tz = self.get_employee_tz(employee)
            local_ci = pytz.utc.localize(check_in).astimezone(emp_tz)
            work_date = local_ci.date()
            schedule = self.get_employee_day_schedule(employee, work_date, emp_tz)
            if schedule:
                check_out = schedule['end'].astimezone(pytz.utc).replace(tzinfo=None)

        return super().calculate_worked_time(check_in, check_out, employee)
