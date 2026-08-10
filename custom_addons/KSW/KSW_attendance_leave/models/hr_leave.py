# -*- coding: utf-8 -*-
from datetime import timedelta

import pytz
from markupsafe import Markup

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools.misc import format_date


class HrLeave(models.Model):
    _inherit = 'hr.leave'

    x_attendance_ids = fields.Many2many(
        'hr.attendance',
        'hr_leave_attendance_rel',
        'leave_id',
        'attendance_id',
        string='Attendance Issues',
        domain="[('employee_id', '=', employee_id), "
               "('x_is_covered', '=', False),"
               "('check_in', '>=', request_date_from), "
               "('check_in', '<=', request_date_to)] + "
               "(['|', ('x_late_minutes', '>', 0), ('x_early_leave_minutes', '>', 0)] "
               "if request_unit_hours else [('x_is_absent', '=', True)])",
        help="Select the attendance records with issues (late, early leave, or absence) "
             "that this time-off request is meant to cover.",
    )

    holiday_status_is_attendance_issue = fields.Boolean(
        related='holiday_status_id.is_attendance_issue',
    )

    def action_submit(self):
        """Called by the 'Submit' button on new records.
        The record is auto-saved by Odoo before this method runs,
        so create() has already set state='confirm' and posted activities.
        Nothing else to do here."""
        return True

    x_total_late_minutes = fields.Float(
        string='Total Late Minutes',
        compute='_compute_attendance_summary',
    )

    x_total_early_leave_minutes = fields.Float(
        string='Total Early Leave Minutes',
        compute='_compute_attendance_summary',
    )

    x_total_absent_days = fields.Integer(
        string='Absent Days',
        compute='_compute_attendance_summary',
    )

    x_attendance_count = fields.Integer(
        string='Attendance Records',
        compute='_compute_attendance_summary',
    )

    x_attendance_line_ids = fields.One2many(
        'hr.leave.attendance.line',
        'leave_id',
        string='Attendance Issue Hours',
    )

    @api.onchange('holiday_status_id')
    def _onchange_holiday_status_clear_attendance(self):
        self.x_attendance_ids = [(5, 0, 0)]
        self.x_attendance_line_ids = [(5, 0, 0)]

    @api.onchange('x_attendance_ids')
    def _onchange_attendance_ids_generate_lines(self):
        """Regenerate hour lines whenever attendance issues change."""
        self._generate_attendance_lines()

    def _generate_attendance_lines(self):
        """Build one hour-range line per issue type per attendance record."""
        self.x_attendance_line_ids = [(5, 0, 0)]
        if not self.x_attendance_ids:
            return

        calendar = self.resource_calendar_id or self.env.company.resource_calendar_id
        new_lines = []
        all_from = []
        all_to = []

        for att in self.x_attendance_ids:
            # Ensure we have a real DB id (not a NewId from the onchange cache)
            att_id = att._origin.id if hasattr(att, '_origin') and att._origin else att.id
            if not att_id or not att.check_in:
                continue
            check_date = att.check_in.date()

            # Get scheduled start / end from resource.calendar.group.line
            group_lines = self._get_group_lines_for_calendar(
                calendar, target_date=check_date,
            )
            if group_lines:
                sched_start = min(group_lines.mapped('hour_from'))
                sched_end = max(group_lines.mapped('hour_to'))
            else:
                sched_start = 8.0
                sched_end = 17.0

            # A night shift is stored wrapped (21:00 -> 5:00).  Unwrap it so the
            # window stays monotonic while minutes are sliced off either end;
            # without this, `sched_end - early_minutes` goes negative and
            # hr.leave._to_utc raises "hour must be in 0..23".
            if sched_end <= sched_start:
                sched_end += 24.0

            if att.x_late_minutes > 0:
                hour_from = sched_start
                hour_to = sched_start + att.x_late_minutes / 60.0
                all_from.append(hour_from)
                all_to.append(hour_to)
                new_lines.append((0, 0, {
                    'attendance_id': att_id,
                    'issue_type': 'late',
                    'hour_from': hour_from % 24.0,
                    'hour_to': hour_to % 24.0,
                    'accepted_minutes': att.x_late_minutes,
                }))

            if att.x_early_leave_minutes > 0:
                hour_from = sched_end - att.x_early_leave_minutes / 60.0
                hour_to = sched_end
                all_from.append(hour_from)
                all_to.append(hour_to)
                new_lines.append((0, 0, {
                    'attendance_id': att_id,
                    'issue_type': 'early_leave',
                    'hour_from': hour_from % 24.0,
                    'hour_to': hour_to % 24.0,
                    'accepted_minutes': att.x_early_leave_minutes,
                }))

        self.x_attendance_line_ids = new_lines

        # Update request_hour_from / request_hour_to so _compute_date_from_to
        # produces a reasonable date range for the leave record.  These keep the
        # *unwrapped* hours — a value past 24 (or below 0) is how the window says
        # "the next/previous calendar day", and _to_utc rolls the date over.
        if new_lines:
            self.request_hour_from = min(all_from)
            self.request_hour_to = max(all_to)

    @api.depends('x_attendance_ids', 'x_attendance_line_ids.accepted_minutes')
    def _compute_attendance_summary(self):
        for leave in self:
            leave.x_total_late_minutes = sum(leave.x_attendance_ids.mapped('x_late_minutes'))
            leave.x_total_early_leave_minutes = sum(leave.x_attendance_ids.mapped('x_early_leave_minutes'))
            leave.x_total_absent_days = len(leave.x_attendance_ids.filtered('x_is_absent'))
            leave.x_attendance_count = len(leave.x_attendance_ids)

    def _get_total_accepted_minutes(self):
        """Return total accepted minutes from attendance lines (hour-based leaves)."""
        self.ensure_one()
        if self.x_attendance_line_ids:
            return sum(self.x_attendance_line_ids.mapped('accepted_minutes'))
        # Fallback to raw attendance minutes if no lines exist yet
        return sum(
            a.x_late_minutes + a.x_early_leave_minutes
            for a in self.x_attendance_ids
        )

    def get_attendance_breakdown(self):
        """Return per-record attendance details for the leave stats widget."""
        result = {}
        for leave in self:
            details = []
            # Build a map of accepted minutes per attendance record from lines
            accepted_map = {}
            for line in leave.x_attendance_line_ids:
                att_id = line.attendance_id.id
                accepted_map.setdefault(att_id, 0.0)
                accepted_map[att_id] += line.accepted_minutes

            for att in leave.x_attendance_ids.sorted(
                lambda a: a.check_in or fields.Datetime.now()
            ):
                if att.x_is_absent:
                    check_date = att.check_in.date() if att.check_in else None
                    hours = leave._get_daily_work_hours(leave.employee_id, check_date)
                else:
                    # Use accepted minutes from lines
                    accepted = accepted_map.get(att.id, att.x_late_minutes + att.x_early_leave_minutes)
                    hours = accepted / 60.0

                h = int(hours)
                m = int(round((hours - h) * 60))

                details.append({
                    'date': att.check_in.strftime('%m/%d/%Y') if att.check_in else '',
                    'hours_display': '%d:%02d' % (h, m),
                    'is_absent': bool(att.x_is_absent),
                })
            result[leave.id] = details
        return result

    @api.depends('x_attendance_ids', 'x_attendance_line_ids.accepted_minutes')
    def _compute_display_name(self):
        """Override to show the full date range for attendance-based leaves."""
        attendance_leaves = self._attendance_issue_leaves()
        remaining = self - attendance_leaves

        if remaining:
            super(HrLeave, remaining)._compute_display_name()

        for leave in attendance_leaves:
            target = leave.employee_id.name or ""
            time_off_type = leave.holiday_status_id.name or _('Time Off')
            duration = leave.duration_display
            record_count = len(leave.x_attendance_ids)

            # Build date range from attendance check_in dates
            check_in_dates = leave.x_attendance_ids.filtered('check_in').mapped(
                lambda a: a.check_in.date()
            )
            if check_in_dates:
                min_date = min(check_in_dates)
                max_date = max(check_in_dates)
                if min_date == max_date:
                    display_date = format_date(self.env, min_date)
                else:
                    display_date = _('%(date_from)s to %(date_to)s',
                        date_from=format_date(self.env, min_date),
                        date_to=format_date(self.env, max_date),
                    )
            else:
                user_tz = pytz.timezone(leave.tz)
                date_from = leave.date_from and leave.date_from.astimezone(user_tz).date()
                display_date = format_date(self.env, date_from) or ""

            if self.env.context.get('short_name'):
                short_name = leave.name or time_off_type
                leave.display_name = _("%(name)s: %(duration)s", name=short_name, duration=duration)
            elif not target:
                leave.display_name = _("%(leave_type)s: %(duration)s (%(start)s)",
                    leave_type=time_off_type, duration=duration, start=display_date)
            else:
                leave.display_name = _(
                    "%(person)s on %(leave_type)s: %(duration)s (%(start)s — %(count)s records)",
                    person=target,
                    leave_type=time_off_type,
                    duration=duration,
                    start=display_date,
                    count=record_count,
                )

    def _attendance_issue_leaves(self):
        """Return the leaves whose duration really is derived from attendance issues.

        `x_attendance_ids` is also filled on ordinary leave types (business trip,
        sick, umrah…) by `_auto_link_absence_attendance()` — there it only marks
        which absence records the leave covers, and must NOT change the duration.
        Duration / display overrides therefore filter on the leave type,
        never on the m2m being non-empty.

        Hour-unit leaves (Late Excuse / Early Excuse) are included even when the
        type is not flagged: they exist solely to excuse late/early minutes and
        their duration must stay driven by the accepted minutes.
        """
        return self.filtered(
            lambda l: l.x_attendance_ids and (
                l.holiday_status_id.is_attendance_issue or l.request_unit_hours
            )
        )

    @api.constrains('x_attendance_ids', 'holiday_status_id')
    def _check_attendance_ids_required(self):
        for leave in self:
            # Only leave types flagged as "Depends on Attendance Issue"
            # require at least one attendance record.
            if not leave.holiday_status_id or not leave.holiday_status_id.is_attendance_issue:
                continue
            if not leave.x_attendance_ids:
                raise ValidationError(_("You must select at least one attendance issue to cover."))

    # ──────────────────────────────────────────────────────────────────
    # Helper: get schedule info from resource.calendar.group.line
    # ──────────────────────────────────────────────────────────────────

    def _get_group_lines_for_calendar(self, calendar, target_date=None, day_period=None):
        """Return resource.calendar.group.line records for the given calendar,
        optionally filtered by target_date and day_period.

        Path: resource.calendar -> calendar_group_ids (m2m) ->
              resource.calendar.group -> line_ids -> resource.calendar.group.line
        """
        if not calendar:
            return self.env['resource.calendar.group.line']

        calendar_groups = calendar.calendar_group_ids
        if not calendar_groups:
            return self.env['resource.calendar.group.line']

        all_lines = calendar_groups.mapped('line_ids').filtered(
            lambda l: l.day_period != 'break'
        )

        if target_date:
            all_lines = all_lines.filtered(
                lambda l: (not l.start_date or l.start_date <= target_date)
                          and (not l.end_date or l.end_date >= target_date)
            )
            day_of_week = str(target_date.weekday())
            all_lines = all_lines.filtered(lambda l: l.dayofweek == day_of_week)

        if day_period:
            period_lines = all_lines.filtered(lambda l: l.day_period == day_period)
            # Also include full_day lines split to the requested period
            full_day_lines = all_lines.filtered(lambda l: l.day_period == 'full_day')
            all_lines = period_lines | full_day_lines

        return all_lines

    def _get_daily_work_hours(self, employee, check_in_date=None):
        """Get daily work hours from resource.calendar.group.line
        via: employee -> resource_calendar_id -> (m2m) resource.calendar.group -> line_ids.
        Break hours are deducted from the total.

        Without `check_in_date` there is no single day to measure, so the
        average over the week's working days is returned.  (Summing the
        unfiltered group lines would yield the WEEKLY total — e.g. 48.5 h — and
        that value used to leak into `number_of_hours`.)
        """
        calendar = employee.resource_calendar_id
        if not calendar:
            return 8.0

        lines = (
            self._get_group_lines_for_calendar(calendar, target_date=check_in_date)
            if check_in_date else self.env['resource.calendar.group.line']
        )

        if not lines:
            if check_in_date:
                return 8.0
            # No specific date: average the week's hours over its working days
            calendar_groups = calendar.calendar_group_ids
            if not calendar_groups:
                return 8.0
            all_lines = calendar_groups.mapped('line_ids').filtered(
                lambda l: l.day_period != 'break'
            )
            if not all_lines:
                return 8.0
            # Also get break lines for deduction
            break_lines = calendar_groups.mapped('line_ids').filtered(
                lambda l: l.day_period == 'break'
            )
            total_weekly_hours = all_lines._duration_hours()
            total_weekly_breaks = break_lines._duration_hours()
            work_days_count = len(set(all_lines.mapped('dayofweek')))
            if work_days_count:
                return (total_weekly_hours - total_weekly_breaks) / work_days_count
            return 8.0

        # Get break lines for the same day to deduct
        break_hours = self._get_break_hours_for_calendar(calendar, target_date=check_in_date)

        return lines._duration_hours() - break_hours

    def _get_break_hours_for_calendar(self, calendar, target_date=None):
        """Return total break hours from resource.calendar.group.line
        for the given calendar and date."""
        if not calendar:
            return 0.0

        calendar_groups = calendar.calendar_group_ids
        if not calendar_groups:
            return 0.0

        break_lines = calendar_groups.mapped('line_ids').filtered(
            lambda l: l.day_period == 'break'
        )

        if target_date:
            break_lines = break_lines.filtered(
                lambda l: (not l.start_date or l.start_date <= target_date)
                          and (not l.end_date or l.end_date >= target_date)
            )
            day_of_week = str(target_date.weekday())
            break_lines = break_lines.filtered(lambda l: l.dayofweek == day_of_week)

        return break_lines._duration_hours()

    # ──────────────────────────────────────────────────────────────────
    # Override _get_hour_from_to to use resource.calendar.group.line
    # instead of resource.calendar.attendance
    # ──────────────────────────────────────────────────────────────────

    def _get_hour_from_to(self, request_date_from, request_date_to, day_period=None):
        """Return the (hour_from, hour_to) for the given request dates using
        resource.calendar.group.line instead of resource.calendar.attendance.
        """
        calendar = self.resource_calendar_id
        if not calendar:
            return (0, 24)

        # --- hour_from: earliest work start on request_date_from ---
        from_lines = self._get_group_lines_for_calendar(
            calendar, target_date=request_date_from, day_period=day_period,
        )
        if from_lines:
            if day_period == 'afternoon':
                hour_from = min(
                    l.hour_from if l.day_period == 'afternoon' else 12.0
                    for l in from_lines
                )
            else:
                hour_from = min(from_lines.mapped('hour_from'))
        else:
            # Fallback: use any lines for this calendar (default schedule)
            fallback = self._get_group_lines_for_calendar(calendar, day_period=day_period)
            hour_from = min(fallback.mapped('hour_from'), default=0.0)

        # --- hour_to: latest work end on request_date_to ---
        to_lines = self._get_group_lines_for_calendar(
            calendar, target_date=request_date_to, day_period=day_period,
        )
        if to_lines:
            if day_period == 'morning':
                hour_to = max(
                    l.hour_to if l.day_period == 'morning' else 12.0
                    for l in to_lines
                )
            else:
                hour_to = max(to_lines.mapped('hour_to'))
            # A night shift is stored wrapped (21:00 -> 5:00), so the day's end
            # is really 05:00 the *next* morning.  Only the full-shift case can
            # be read off the lines like this — a half-day request substitutes
            # 12.0 above, which says nothing about whether the shift wraps.
            if not day_period and hour_to <= min(to_lines.mapped('hour_from')):
                hour_to += 24.0
        else:
            fallback = self._get_group_lines_for_calendar(calendar, day_period=day_period)
            hour_to = max(fallback.mapped('hour_to'), default=24.0)

        # Whatever the unit, the window must never close before it opens:
        # _check_date rejects that with "The start date must be before or equal
        # to the end date".  _to_utc turns the 24+ hour back into a next-day
        # datetime.
        if hour_to <= hour_from:
            hour_to += 24.0

        return (hour_from, hour_to)

    def _to_utc(self, date, hour, resource):
        """Roll hours outside 0..24 onto the neighbouring calendar day.

        Core calls `float_to_time(hour)`, which raises `ValueError: hour must be
        in 0..23` for anything at or past midnight.  Overnight schedules produce
        exactly that: a 21:00-05:00 shift ends at hour 29.0 of its start day, and
        an excuse slicing minutes off such a shift can land before hour 0.  Both
        simply mean "the next / previous day".
        """
        day_offset, hour = divmod(float(hour), 24.0)
        if day_offset:
            date = date + timedelta(days=int(day_offset))
        return super()._to_utc(date, hour, resource)

    # ──────────────────────────────────────────────────────────────────
    # Override duration computation for attendance-based leaves
    # ──────────────────────────────────────────────────────────────────

    @api.depends('x_attendance_ids', 'x_attendance_line_ids.accepted_minutes')
    def _compute_duration(self):
        """Override to compute duration from accepted minutes for attendance-based leaves,
        and group-line working days for other non-attendance leaves.
        Annual-leave calendar-day logic is handled by KSW_annual_leave."""
        leaves_with_attendance = self._attendance_issue_leaves()
        remaining = self - leaves_with_attendance

        if remaining:
            # Non-attendance leaves: delegate to super (which may include
            # KSW_annual_leave for is_annual_leave types), then group-line fallback
            super(HrLeave, remaining)._compute_duration()
            for leave in remaining:
                if leave.number_of_days == 0 and leave.date_from and leave.date_to:
                    days, hours = self._compute_days_from_group_lines(leave)
                    if days > 0:
                        leave.number_of_days = days
                        leave.number_of_hours = hours

        # x_exceeds_annual_balance lives in KSW_annual_leave, which depends on
        # this module — it is absent while this module's own tests run.
        has_annual_flag = 'x_exceeds_annual_balance' in self._fields

        for leave in leaves_with_attendance:
            if has_annual_flag:
                leave.x_exceeds_annual_balance = False
            if leave.request_unit_hours:
                total_accepted = leave._get_total_accepted_minutes()
                total_hours = total_accepted / 60.0
                daily_hours_list = []
                for att in leave.x_attendance_ids:
                    check_date = att.check_in.date() if att.check_in else None
                    daily_hours_list.append(leave._get_daily_work_hours(leave.employee_id, check_date))
                avg_daily = sum(daily_hours_list) / len(daily_hours_list) if daily_hours_list else 8.0
                # A schedule that measures to nothing must not divide by zero or
                # flip the sign of number_of_days (its SQL constraint is >= 0).
                if avg_daily <= 0:
                    avg_daily = 8.0
                leave.number_of_days = total_hours / avg_daily
                leave.number_of_hours = total_hours
            else:
                absent_count = leave.x_total_absent_days or 0
                absent_hours = absent_count * leave._get_daily_work_hours(leave.employee_id)
                leave.number_of_days = absent_count
                leave.number_of_hours = absent_hours

    def _leave_local_dates(self, leave):
        """Return the (start, end) dates of a leave as the user requested them.

        `date_from` / `date_to` are UTC datetimes — in Riyadh a one-day leave is
        stored as 21:00 of the previous day → 20:59 of the day itself, so taking
        `.date()` off them counts one extra day.  `request_date_from/to` are
        plain dates and are what the form shows, so prefer them.
        """
        start = leave.request_date_from
        end = leave.request_date_to or start
        if not start:
            start = leave.date_from.date() if leave.date_from else None
            end = leave.date_to.date() if leave.date_to else start
        return (start, end)

    def _compute_days_from_group_lines(self, leave):
        """Count working days over the leave's date range using
        resource.calendar.group.line when standard attendance_ids are empty."""
        start, end = self._leave_local_dates(leave)
        return self._count_group_line_days(leave.employee_id, start, end)

    def _count_group_line_days(self, employee, start, end):
        """(days, hours) between two dates from resource.calendar.group.line."""
        calendar = employee.resource_calendar_id
        if not calendar or not calendar.calendar_group_ids:
            return (0, 0)

        # Determine which weekdays are work days from group lines
        all_lines = calendar.calendar_group_ids.mapped('line_ids').filtered(
            lambda l: l.day_period != 'break'
        )
        if not all_lines:
            return (0, 0)

        work_weekdays = set(int(d) for d in all_lines.mapped('dayofweek'))

        # Count working days in the date range
        from datetime import timedelta
        if not start or not end:
            return (0, 0)

        work_days = 0
        covered = False       # any group line valid on any day of the range
        current = start
        while current <= end:
            day_lines = all_lines.filtered(
                lambda l, d=current: (not l.start_date or l.start_date <= d)
                                     and (not l.end_date or l.end_date >= d)
            )
            if day_lines:
                covered = True
                if current.weekday() in work_weekdays and day_lines.filtered(
                    lambda l, d=current: l.dayofweek == str(d.weekday())
                ):
                    work_days += 1
            current += timedelta(days=1)

        # Calculate average daily hours for the total hours figure
        daily_hours = self._get_daily_work_hours(employee) or 8.0

        if not work_days and not covered:
            # The work schedule says nothing about this period at all — usually
            # a schedule group whose lines expired and were never extended.
            # Falling through with 0 would display "0 days" for a real absence,
            # so count calendar days instead.
            cal_days = (end - start).days + 1
            return (cal_days, cal_days * daily_hours)

        return (work_days, work_days * daily_hours)

    def _get_durations(self, check_leave_type=True, resource_calendar=None):
        """Override to use accepted minutes for attendance-based leaves,
        and group lines fallback for others."""
        attendance_leaves = self._attendance_issue_leaves()
        remaining = self - attendance_leaves

        result = {}
        if remaining:
            # Non-attendance: base + group-line fallback
            result.update(super(HrLeave, remaining)._get_durations(
                check_leave_type=check_leave_type,
                resource_calendar=resource_calendar,
            ))
            for leave in remaining:
                days, hours = result.get(leave.id, (0, 0))
                if days == 0 and leave.date_from and leave.date_to:
                    gl_days, gl_hours = self._compute_days_from_group_lines(leave)
                    if gl_days > 0:
                        result[leave.id] = (gl_days, gl_hours)

        for leave in attendance_leaves:
            if leave.request_unit_hours:
                total_accepted = leave._get_total_accepted_minutes()
                total_hours = total_accepted / 60.0
                daily_hours_list = []
                for att in leave.x_attendance_ids:
                    check_date = att.check_in.date() if att.check_in else None
                    daily_hours_list.append(leave._get_daily_work_hours(leave.employee_id, check_date))
                avg_daily = sum(daily_hours_list) / len(daily_hours_list) if daily_hours_list else 8.0
                total_days = total_hours / avg_daily
                result[leave.id] = (total_days, total_hours)
            else:
                absent_count = len(leave.x_attendance_ids.filtered('x_is_absent'))
                absent_hours = absent_count * leave._get_daily_work_hours(leave.employee_id)
                result[leave.id] = (absent_count, absent_hours)

        return result

    # ──────────────────────────────────────────────────────────────────
    # Override approval / validation to bypass schedule-based checks
    # for attendance-based leaves
    # ──────────────────────────────────────────────────────────────────

    def action_approve(self, check_state=True):
        """Bypass schedule-based checks during approval, then mark attendance as covered.

        The "Validate" button on a second-approval (validate1) leave also calls
        action_approve (see hr_holidays views) — core dispatches it to
        _action_validate internally. Route on state the same way: leaves
        already at validate1 go to _action_validate, leaves still at confirm
        go through the attendance-based first-step method. Without this split,
        a second approver clicking Validate on a validate1 leave was forced
        through the confirm-only method and always hit "must be confirmed".
        """
        attendance_leaves = self.filtered('x_attendance_ids')

        if attendance_leaves:
            leave_to_validate = attendance_leaves.filtered(lambda l: l.state == 'validate1')
            leave_to_approve = attendance_leaves - leave_to_validate

            if leave_to_approve:
                leave_to_approve._action_approve_attendance_based(check_state=check_state)
            if leave_to_validate:
                leave_to_validate._action_validate(check_state)
            if not self.env.context.get('leave_fast_create'):
                attendance_leaves.activity_update()

        remaining = self - attendance_leaves
        res = True
        if remaining:
            res = super(HrLeave, remaining).action_approve(check_state=check_state)

        return res

    def _action_approve_attendance_based(self, check_state=True):
        """Custom approval logic that skips resource.calendar.attendance checks."""
        for leave in self:
            if check_state and leave.state != 'confirm':
                raise ValidationError(
                    _('Time off request must be confirmed ("To Approve") '
                      'in order to approve it.')
                )

            current_employee = self.env.user.employee_id
            # Disable tracking — our custom chatter message replaces automatic ones
            leave_no_track = leave.with_context(tracking_disable=True)

            if leave.holiday_status_id.leave_validation_type == 'both':
                if not leave.env.user.has_group('hr_holidays.group_hr_holidays_manager'):
                    leave_no_track.write({
                        'state': 'validate1',
                        'first_approver_id': current_employee.id,
                    })
                else:
                    leave_no_track.write({
                        'state': 'validate',
                        'first_approver_id': current_employee.id,
                        'second_approver_id': current_employee.id,
                    })
            else:
                leave_no_track.write({
                    'state': 'validate',
                    'first_approver_id': current_employee.id,
                })

            # x_is_covered is a computed field that depends on x_leave_ids.state
            # It recomputes automatically when the leave state changes to 'validate'

            # Validate (create resource leave, calendar event, etc.)
            leave._validate_leave_request()

    def _action_validate(self, check_state=True):
        """Override to skip 'not supposed to work' check for attendance-based leaves."""
        attendance_leaves = self.filtered('x_attendance_ids')
        remaining = self - attendance_leaves

        if remaining:
            super(HrLeave, remaining)._action_validate(check_state=check_state)

        if attendance_leaves:
            # Skip _get_leaves_on_public_holiday check — attendance records
            # already prove the employee was supposed to work.
            current_employee = self.env.user.employee_id
            # Disable tracking — our custom chatter message replaces automatic ones
            att_no_track = attendance_leaves.with_context(tracking_disable=True)
            att_no_track.write({'state': 'validate'})

            second = attendance_leaves.filtered(lambda l: l.validation_type == 'both')
            first = attendance_leaves - second
            second.with_context(tracking_disable=True).write({'second_approver_id': current_employee.id})
            first.with_context(tracking_disable=True).write({'first_approver_id': current_employee.id})

            attendance_leaves._validate_leave_request()
            if not self.env.context.get('leave_fast_create'):
                attendance_leaves.filtered(
                    lambda h: h.validation_type != 'no_validation'
                ).activity_update()

    def _get_leaves_on_public_holiday(self):
        """Override: attendance-based leaves are never 'on public holiday'
        because the employee already has attendance records proving work."""
        remaining = self.filtered(lambda l: not l.x_attendance_ids)
        return super(HrLeave, remaining)._get_leaves_on_public_holiday()

    def _check_approval_update(self, state, raise_if_not_possible=True):
        """Override: the base method only checks state transitions and
        permissions (no schedule logic), so we call super() for ALL records
        to keep the workflow buttons (Approve / Validate) correct."""
        return super()._check_approval_update(state, raise_if_not_possible=raise_if_not_possible)

    def _get_number_of_days(self, date_from, date_to, employee_id):
        """Override to return attendance-based day count using accepted minutes,
        and group lines for other non-attendance leaves."""
        if self and self._attendance_issue_leaves():
            if self.request_unit_hours:
                total_accepted = self._get_total_accepted_minutes()
                total_hours = total_accepted / 60.0
                daily_hours_list = []
                for att in self.x_attendance_ids:
                    check_date = att.check_in.date() if att.check_in else None
                    daily_hours_list.append(self._get_daily_work_hours(
                        self.env['hr.employee'].browse(employee_id), check_date
                    ))
                avg_daily = sum(daily_hours_list) / len(daily_hours_list) if daily_hours_list else 8.0
                return {
                    'days': total_hours / avg_daily,
                    'hours': total_hours,
                }
            else:
                absent_count = len(self.x_attendance_ids.filtered('x_is_absent'))
                return {
                    'days': absent_count,
                    'hours': absent_count * self._get_daily_work_hours(
                        self.env['hr.employee'].browse(employee_id)
                    ),
                }

        # Non-attendance: compute from group lines
        result = {'days': 0, 'hours': 0}
        if date_from and date_to and employee_id:
            employee = self.env['hr.employee'].browse(employee_id)
            if self:
                start, end = self._leave_local_dates(self[0])
            else:
                start = date_from.date() if hasattr(date_from, 'date') else date_from
                end = date_to.date() if hasattr(date_to, 'date') else date_to
            days, hours = self._count_group_line_days(employee, start, end)
            if days > 0:
                result = {'days': days, 'hours': hours}
        return result

    @api.constrains('date_from', 'date_to', 'employee_id')
    def _check_date(self):
        """Override to skip overlap/schedule checks for attendance-based leaves."""
        remaining = self.filtered(lambda l: not l.x_attendance_ids)
        if remaining:
            super(HrLeave, remaining)._check_date()

    @api.constrains('date_from', 'date_to')
    def _check_contracts(self):
        """Override to skip contract-calendar checks for attendance-based leaves."""
        remaining = self.filtered(lambda l: not l.x_attendance_ids)
        if remaining:
            super(HrLeave, remaining)._check_contracts()

    def write(self, vals):
        """Suppress automatic field tracking for attendance-based leaves.
        Custom chatter messages (create / approve / refuse) already provide
        detailed information, so the generic tracking notes like
        '0.46 → 0.52 (Duration)' are redundant noise."""
        if not self.env.context.get('tracking_disable'):
            att_leaves = self.filtered('x_attendance_ids')
            if att_leaves:
                non_att = self - att_leaves
                if non_att:
                    super(HrLeave, non_att).write(vals)
                return super(HrLeave, att_leaves.with_context(tracking_disable=True)).write(vals)
        return super().write(vals)

    def _compute_field_value(self, field):
        """Discard tracking that mail.thread._compute_field_value prepares
        for attendance-based leaves.  This prevents the ORM-triggered
        recompute of number_of_days / number_of_hours from posting noisy
        '0.52 → 0.54 (Duration)' messages in the chatter.

        Also passes leave_skip_state_check in context to avoid
        _check_date_state ValidationError when validated leaves have
        their computed fields recomputed during module upgrades.
        """
        result = super(
            HrLeave, self.with_context(leave_skip_state_check=True)
        )._compute_field_value(field)
        if getattr(field, 'tracking', False) and not self.env.context.get('tracking_disable'):
            att_leaves = self.filtered('x_attendance_ids')
            if att_leaves:
                att_leaves._track_discard()
        return result

    # ──────────────────────────────────────────────────────────────────
    # Chatter messages for attendance-based leaves
    # ──────────────────────────────────────────────────────────────────

    def _build_attendance_details_html(self, show_accepted=False):
        """Build an HTML table of attendance issue details for chatter."""
        self.ensure_one()
        if not self.x_attendance_ids:
            return Markup('')

        rows = Markup('')
        # Build accepted-minutes map from lines
        accepted_map = {}
        for line in self.x_attendance_line_ids:
            att_id = line.attendance_id.id
            accepted_map.setdefault(att_id, [])
            accepted_map[att_id].append(line)

        for att in self.x_attendance_ids.sorted(
            lambda a: a.check_in or fields.Datetime.now()
        ):
            date_str = att.check_in.strftime('%m/%d/%Y') if att.check_in else 'N/A'
            issues = []
            if att.x_is_absent:
                issues.append('Absent')
            if att.x_late_minutes > 0:
                h = int(att.x_late_minutes // 60)
                m = int(att.x_late_minutes % 60)
                issues.append('Late %d:%02d' % (h, m))
            if att.x_early_leave_minutes > 0:
                h = int(att.x_early_leave_minutes // 60)
                m = int(att.x_early_leave_minutes % 60)
                issues.append('Early Leave %d:%02d' % (h, m))
            issue_str = ', '.join(issues) if issues else 'No issue'

            row_html = Markup(
                '<tr>'
                '<td style="padding:4px 8px;">%(date)s</td>'
                '<td style="padding:4px 8px;">%(issue)s</td>'
            ) % {'date': date_str, 'issue': issue_str}

            if show_accepted:
                att_lines = accepted_map.get(att.id, [])
                if att_lines:
                    total_accepted = sum(l.accepted_minutes for l in att_lines)
                    ah = int(total_accepted // 60)
                    am = int(total_accepted % 60)
                    accepted_str = '%d:%02d' % (ah, am)
                else:
                    accepted_str = '—'
                row_html += Markup(
                    '<td style="padding:4px 8px;font-weight:bold;color:#017e84;">✔ %(acc)s</td>'
                ) % {'acc': accepted_str}

            row_html += Markup('</tr>')
            rows += row_html

        # Build header
        if show_accepted:
            header = Markup(
                '<tr style="background:#f0f0f0;">'
                '<th style="padding:4px 8px;">Date</th>'
                '<th style="padding:4px 8px;">Issue</th>'
                '<th style="padding:4px 8px;">Accepted</th>'
                '</tr>'
            )
        else:
            header = Markup(
                '<tr style="background:#f0f0f0;">'
                '<th style="padding:4px 8px;">Date</th>'
                '<th style="padding:4px 8px;">Issue</th>'
                '</tr>'
            )

        return Markup(
            '<table style="border-collapse:collapse;width:100%%;border:1px solid #ddd;">'
            '%(header)s%(rows)s'
            '</table>'
        ) % {'header': header, 'rows': rows}

    @api.model_create_multi
    def create(self, vals_list):
        """Override to post detailed chatter message for attendance-based leaves."""
        holidays = super().create(vals_list)

        for holiday in holidays:
            if holiday.x_attendance_ids and not self.env.context.get('leave_fast_create'):
                details_html = holiday._build_attendance_details_html(show_accepted=False)
                employee_name = holiday.employee_id.name or ''
                leave_type_name = holiday.holiday_status_id.display_name or ''
                record_count = len(holiday.x_attendance_ids)

                body = Markup(
                    '<strong>📋 Time Off Created — Attendance Issues</strong><br/>'
                    '<b>Employee:</b> %(employee)s<br/>'
                    '<b>Leave Type:</b> %(leave_type)s<br/>'
                    '<b>Records:</b> %(count)s<br/><br/>'
                    '%(details)s'
                ) % {
                    'employee': employee_name,
                    'leave_type': leave_type_name,
                    'count': record_count,
                    'details': details_html,
                }
                holiday.message_post(
                    body=body,
                    subtype_xmlid='mail.mt_note',
                )
        return holidays

    def _auto_link_absence_attendance(self):
        """Link existing biometric absence records to a freshly validated regular leave.

        Called from _validate_leave_request for non-attendance-issue leave types
        (annual, sick, business trip, etc.) so that absence records already in
        hr.attendance for the leave date range get their x_leave_ids populated,
        which in turn drives x_is_covered and x_net_is_absent via compute.

        Absence records created by the biometric sync use check_in = midnight UTC,
        which can fall before leave.date_from (the work-start time stored in UTC).
        We widen the lower bound to start-of-UTC-day so these records are found.
        """
        HrAttendance = self.env['hr.attendance'].sudo()
        for leave in self:
            if not leave.employee_id or not leave.date_from or not leave.date_to:
                continue
            # Widen to full UTC days so midnight-UTC absence records aren't missed
            date_from_sod = leave.date_from.replace(hour=0, minute=0, second=0, microsecond=0)
            date_to_eod = leave.date_to.replace(hour=23, minute=59, second=59, microsecond=0)
            absences = HrAttendance.search([
                ('employee_id', '=', leave.employee_id.id),
                ('x_is_absent', '=', True),
                ('x_is_covered', '=', False),
                ('check_in', '>=', date_from_sod),
                ('check_in', '<=', date_to_eod),
            ])
            if absences:
                leave.with_context(tracking_disable=True).write({
                    'x_attendance_ids': [(4, att.id) for att in absences]
                })
                # Force-recompute deductions so x_deduction_amount is zeroed
                # out in this transaction before any payslip reads it.
                absences._recompute_deductions()

    def _validate_leave_request(self):
        """Override to post detailed approval message for attendance-based leaves."""
        attendance_leaves = self.filtered('x_attendance_ids')
        remaining = self - attendance_leaves

        if remaining:
            super(HrLeave, remaining)._validate_leave_request()
            # For regular (non-attendance-issue) leaves, auto-link any absence
            # records that fall within the leave date range so they show covered.
            regular = remaining.filtered(
                lambda l: not l.holiday_status_id.is_attendance_issue
            )
            if regular:
                regular._auto_link_absence_attendance()

        if attendance_leaves:
            # Create resource leaves and calendar meetings
            # Use tracking_disable to prevent duration-change tracking messages
            att_holidays = attendance_leaves.filtered("employee_id")
            att_holidays.with_context(tracking_disable=True)._create_resource_leave()

            meeting_holidays = att_holidays.filtered(
                lambda l: l.holiday_status_id.create_calendar_meeting
            )
            if meeting_holidays:
                from odoo.tools.misc import clean_context
                Meeting = self.env['calendar.event']
                Meeting.check_access('create')
                meeting_values_for_user_id = meeting_holidays._prepare_holidays_meeting_values()
                meetings = self.env['calendar.event']
                for user_id, meeting_values in meeting_values_for_user_id.items():
                    meetings += Meeting.with_user(user_id or self.env.uid).sudo().with_context(
                        clean_context({
                            **self.env.context,
                            **dict(
                                allowed_company_ids=[],
                                no_mail_to_attendees=True,
                                calendar_no_videocall=True,
                                active_model=self._name,
                            ),
                        })
                    ).create(meeting_values)
                Holiday = self.env['hr.leave']
                for meeting in meetings:
                    Holiday.browse(meeting.res_id).meeting_id = meeting

            # Post detailed approval message per leave
            approver_name = self.env.user.name or 'System'
            for holiday in att_holidays:
                details_html = holiday._build_attendance_details_html(show_accepted=True)

                total_accepted = holiday._get_total_accepted_minutes()
                th = int(total_accepted // 60)
                tm = int(total_accepted % 60)
                total_display = '%d:%02d hours' % (th, tm)

                employee_name = holiday.employee_id.name or ''
                leave_type_name = holiday.holiday_status_id.display_name or ''
                record_count = len(holiday.x_attendance_ids)
                notify_partner_ids = holiday.employee_id.user_id.partner_id.ids

                body = Markup(
                    '<strong>✅ Time Off Approved — Attendance Issues</strong><br/>'
                    '<b>Employee:</b> %(employee)s<br/>'
                    '<b>Leave Type:</b> %(leave_type)s<br/>'
                    '<b>Records:</b> %(count)s<br/>'
                    '<b>Approved by:</b> %(approver)s<br/><br/>'
                    '%(details)s'
                    '<br/><div style="margin-top:8px;padding:6px 12px;'
                    'background:#e8f5e9;border-radius:4px;display:inline-block;">'
                    '<strong>Total Accepted:</strong> %(total)s'
                    '</div>'
                ) % {
                    'employee': employee_name,
                    'leave_type': leave_type_name,
                    'count': record_count,
                    'approver': approver_name,
                    'details': details_html,
                    'total': total_display,
                }

                holiday.message_post(
                    body=body,
                    partner_ids=notify_partner_ids,
                    subtype_xmlid='mail.mt_comment',
                )

        # Synchronously recompute coverage and deduction amounts for every
        # attendance record linked to this batch of leaves.  The ORM trigger
        # chain (x_leave_ids.state → _compute_net_minutes) is registered but
        # can be deferred past the payslip-creation transaction; this call
        # guarantees x_deduction_amount is zeroed out in the DB right now.
        all_atts = self.mapped('x_attendance_ids')
        if all_atts:
            all_atts._recompute_deductions()

        # A covered absence counts as an attended workday for the adjacent
        # weekend grant, so approving this leave can entitle the employee to a
        # Friday/Saturday the nightly cron already decided against.
        self._recheck_weekend_grants()

    def _recheck_weekend_grants(self):
        """Re-open the weekend-grant decision for the days around these leaves.

        The grant asks whether the workday immediately before/after a weekend
        block was attended, and a validated leave flips that answer (a covered
        absence counts as attended).  The nightly cron takes that decision once,
        the day after — long before the leave is usually approved — and never
        revisits it, so without this the granted Friday is simply never created.

        Also covers the reverse direction: refusing or resetting a leave
        uncovers the absence, and the same pass revokes the now-unearned grant.
        """
        # x_is_covered is a stored compute driven by x_leave_ids.state; the
        # weekend pass reads it straight from the DB, so it has to be written
        # out before we hand over.
        self.env.flush_all()
        self.env['biometric.attendance.sync']._regenerate_weekends_for_leaves(self)

    def action_refuse(self):
        """Unmark attendance records when leave is refused, post detailed message."""
        attendance_leaves = self.filtered('x_attendance_ids')
        if attendance_leaves:
            refuser_name = self.env.user.name or 'System'
            for leave in attendance_leaves:
                # x_is_covered recomputes automatically when leave state changes to 'refuse'
                details_html = leave._build_attendance_details_html(show_accepted=True)
                employee_name = leave.employee_id.name or ''
                leave_type_name = leave.holiday_status_id.display_name or ''
                record_count = len(leave.x_attendance_ids)

                body = Markup(
                    '<strong>❌ Time Off Refused — Attendance Issues</strong><br/>'
                    '<b>Employee:</b> %(employee)s<br/>'
                    '<b>Leave Type:</b> %(leave_type)s<br/>'
                    '<b>Records:</b> %(count)s<br/>'
                    '<b>Refused by:</b> %(refuser)s<br/><br/>'
                    '%(details)s'
                ) % {
                    'employee': employee_name,
                    'leave_type': leave_type_name,
                    'count': record_count,
                    'refuser': refuser_name,
                    'details': details_html,
                }
                notify_partner_ids = leave.employee_id.user_id.partner_id.ids
                leave.message_post(
                    body=body,
                    partner_ids=notify_partner_ids,
                    subtype_xmlid='mail.mt_comment',
                )
        # Use tracking_disable for attendance leaves to suppress redundant tracking
        remaining = self - attendance_leaves
        if remaining:
            super().action_refuse()
        if attendance_leaves:
            # Replicate base refuse logic with tracking disabled
            current_employee = self.env.user.employee_id
            att_no_track = attendance_leaves.with_context(tracking_disable=True)
            validated = attendance_leaves.filtered(lambda h: h.state == 'validate1')
            validated.with_context(tracking_disable=True).write({
                'state': 'refuse',
                'first_approver_id': current_employee.id,
            })
            (attendance_leaves - validated).with_context(tracking_disable=True).write({
                'state': 'refuse',
                'second_approver_id': current_employee.id,
            })
            attendance_leaves.mapped('meeting_id').write({'active': False})
            attendance_leaves.activity_update()
            # Restore deductions now that the leave is refused (no longer validated).
            attendance_leaves.mapped('x_attendance_ids')._recompute_deductions()
        # The days are uncovered again — revoke any weekend they had earned.
        self._recheck_weekend_grants()
        return True

    def action_draft(self):
        """Reset leave to initial state ('confirm').
        Base Odoo 19 hr.leave does not have action_draft or 'draft' state.
        The initial state is 'confirm' (To Approve).
        """
        if any(hol.state not in ('confirm', 'refuse') for hol in self):
            raise UserError(_('Time off request state must be "Confirmed" or '
                              '"Refused" in order to reset to Draft.'))
        self.write({
            'state': 'confirm',
            'first_approver_id': False,
            'second_approver_id': False,
        })
        # Leave is no longer validated — restore deductions on linked records.
        self.mapped('x_attendance_ids')._recompute_deductions()
        self._recheck_weekend_grants()
        self.activity_update()
        return True
