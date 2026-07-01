# -*- coding: utf-8 -*-
import logging
from datetime import datetime as dt, timedelta

import pytz

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# ZK device clocks routinely drift behind the Odoo server clock.  If a
# punch happens within this window of the last cron run, its UTC timestamp
# can fall at-or-before `last_download_time` and be silently skipped by the
# `utc_time <= cutoff_time` filter — permanently, because the cutoff only
# moves forward.  Re-processing the last N hours is harmless: the SQL upsert
# in `_sync_attendance_record` will just UPDATE existing rows to the same
# values.
_INCREMENTAL_LOOKBACK_HOURS = 4

# How many days before/after a weekend block to search for an attended workday.
# A window of 5 covers Mon-Thu for a Thu/Fri weekend even when the immediately
# adjacent day is missing from the sync.
_ADJACENT_WINDOW = 5


class BiometricAttendanceSyncKSW(models.AbstractModel):
    _inherit = 'biometric.attendance.sync'

    @api.model
    def _process_raw_logs(self, attendance_logs, valid_bio_ids, device_tz,
                          cutoff_time, year_start, year_end, batch_size):
        """Apply a lookback buffer to the incremental cutoff.

        Shifts the effective cutoff back by _INCREMENTAL_LOOKBACK_HOURS so
        records whose device-side timestamp is slightly earlier than
        `last_download_time` (due to clock drift) are always re-fetched.
        """
        if cutoff_time:
            cutoff_time = cutoff_time - timedelta(hours=_INCREMENTAL_LOOKBACK_HOURS)
        return super()._process_raw_logs(
            attendance_logs, valid_bio_ids, device_tz,
            cutoff_time, year_start, year_end, batch_size)

    @api.model
    def _fetch_biometric_attendance(self, device, incremental=False):
        """Clear the stale-sync alert flag once a device reconnects.

        last_download_time only advances on a successful connect, so this
        runs precisely when the outage that triggered an alert (see
        biometric_device_details.py::cron_check_stale_devices) is over.
        """
        result = super()._fetch_biometric_attendance(device, incremental=incremental)
        if incremental and device.x_stale_alert_sent:
            device.x_stale_alert_sent = False
        return result

    def _sync_attendance_record(self, new_cr, env, emp_id, day, times, emp_tz):
        created, updated = super()._sync_attendance_record(
            new_cr, env, emp_id, day, times, emp_tz)

        # Zero out late/early penalties for schedules that opt out of
        # lateness tracking (x_skip_attendance_issues). Absences are kept.
        employee = env['hr.employee'].browse(emp_id)
        if employee.resource_calendar_id.x_skip_attendance_issues:
            new_cr.execute(
                "UPDATE hr_attendance "
                "SET x_late_minutes = 0.0, x_early_leave_minutes = 0.0 "
                "WHERE employee_id = %s "
                "  AND check_in >= %s AND check_in < %s",
                (emp_id,
                 day.strftime("%Y-%m-%d"),
                 (day + timedelta(days=1)).strftime("%Y-%m-%d")))

        # Patch net columns = raw columns for the record just inserted/updated.
        # Skip records already covered by a validated leave so we don't clobber
        # accepted-minutes that _compute_net_minutes stored on approval.
        new_cr.execute(
            "UPDATE hr_attendance ha "
            "SET x_net_late_minutes = ha.x_late_minutes, "
            "    x_net_early_leave_minutes = ha.x_early_leave_minutes, "
            "    x_net_worked_hours = ha.worked_hours, "
            "    x_net_is_absent = ha.x_is_absent "
            "WHERE ha.employee_id = %s "
            "  AND ha.check_in >= %s AND ha.check_in < %s "
            "  AND NOT EXISTS ("
            "      SELECT 1 FROM hr_leave_attendance_rel rel "
            "      JOIN hr_leave hl ON hl.id = rel.leave_id "
            "      WHERE rel.attendance_id = ha.id AND hl.state = 'validate'"
            "  )",
            (emp_id,
             day.strftime("%Y-%m-%d"),
             (day + timedelta(days=1)).strftime("%Y-%m-%d")))

        return created, updated

    @api.model
    def _generate_weekend_records(self, employees, date_from, date_to):
        """Override: fix overnight-shift and late-sync issues.

        Two fixes over upstream:

        1. Night-shift crash: upstream extracts only hour/minute from
           ref_schedule['end'] and places it on grant_day, discarding the
           +1-day offset that get_employee_day_schedule added for overnight
           schedules.  That makes check_out < check_in → ValidationError.
           Fix: add timedelta(days=1) when sched_end <= sched_start.

        2. Late-sync gap: upstream requires the immediately adjacent workday
           (Thursday for a Fri weekend) to already be in the attendance table.
           When the device sync runs after this function, the Thursday punch
           hasn't arrived yet, so the Friday is silently skipped and never
           re-created on subsequent runs (because existing_dates blocks it).
           Fix: search for the nearest attended workday within _ADJACENT_WINDOW
           days in each direction instead of requiring the exact adjacent day.
           The attendance query is widened by the same window so those earlier
           days are actually in memory.
        """
        helper = self.env['biometric.schedule.helper']
        HrAttendance = self.env['hr.attendance']
        total_created = 0

        for emp in employees:
            emp_tz = helper.get_employee_tz(emp)

            all_attendances = HrAttendance.search([
                ('employee_id', '=', emp.id),
                ('check_in', '>=', dt.combine(
                    date_from - timedelta(days=_ADJACENT_WINDOW), dt.min.time())),
                ('check_in', '<', dt.combine(
                    date_to + timedelta(days=_ADJACENT_WINDOW + 1), dt.min.time())),
                '|',
                ('x_is_absent', '=', False),
                ('x_is_covered', '=', True),  # covered absences count as attended for weekend grant
            ])
            attended_dates = set()
            existing_dates = set()
            for att in all_attendances:
                att_date = att.check_in.date() if isinstance(att.check_in, dt) else att.check_in
                existing_dates.add(att_date)
                # Real punches and leave-covered absences both qualify as "attended"
                # for the adjacent-day weekend grant check (leave = worked for payroll).
                if not att.x_is_weekend and (not att.x_is_absent or att.x_is_covered):
                    attended_dates.add(att_date)

            current = date_from
            while current <= date_to:
                if helper.is_scheduled_workday(emp, current):
                    current += timedelta(days=1)
                    continue

                weekend_start = current
                weekend_end = current
                while (weekend_end + timedelta(days=1)) <= date_to \
                        and not helper.is_scheduled_workday(emp, weekend_end + timedelta(days=1)):
                    weekend_end += timedelta(days=1)

                # Grant decision: the workday IMMEDIATELY before or after
                # the block must be attended.  The 5-day window is used
                # only to find a reference schedule for the check-in times.
                day_before = weekend_start - timedelta(days=1)
                day_after = weekend_end + timedelta(days=1)

                if day_before not in attended_dates and day_after not in attended_dates:
                    # Both adjacent workdays are absent: revoke any stale
                    # grant records for this block and skip.
                    stale = HrAttendance.sudo().search([
                        ('employee_id', '=', emp.id),
                        ('x_is_weekend', '=', True),
                        ('check_in', '>=', dt.combine(
                            weekend_start, dt.min.time())),
                        ('check_in', '<', dt.combine(
                            weekend_end + timedelta(days=1), dt.min.time())),
                    ])
                    if stale:
                        stale.unlink()
                    current = weekend_end + timedelta(days=1)
                    continue

                # Find nearest attended workday within the window on each
                # side — used only for the schedule reference (check-in
                # hours), not for the grant decision above.
                nearest_before = next(
                    (weekend_start - timedelta(days=i)
                     for i in range(1, _ADJACENT_WINDOW + 1)
                     if (weekend_start - timedelta(days=i)) in attended_dates),
                    None,
                )
                nearest_after = next(
                    (weekend_end + timedelta(days=i)
                     for i in range(1, _ADJACENT_WINDOW + 1)
                     if (weekend_end + timedelta(days=i)) in attended_dates),
                    None,
                )

                if nearest_before is not None or nearest_after is not None:
                    ref_day = nearest_before if nearest_before is not None else nearest_after
                    grant_day = weekend_start
                    while grant_day <= weekend_end:
                        # For calendars that treat Saturday as a required
                        # workday, suppress the weekend grant so the day
                        # becomes unpresented and is deducted normally.
                        if grant_day.weekday() == 5:
                            emp_cal = (
                                emp.resource_calendar_id
                                or emp.company_id.resource_calendar_id
                            )
                            if emp_cal and emp_cal.x_saturday_required:
                                stale = HrAttendance.sudo().search([
                                    ('employee_id', '=', emp.id),
                                    ('x_is_weekend', '=', True),
                                    ('check_in', '>=', dt.combine(
                                        grant_day, dt.min.time())),
                                    ('check_in', '<', dt.combine(
                                        grant_day + timedelta(days=1),
                                        dt.min.time())),
                                ])
                                if stale:
                                    stale.unlink()
                                grant_day += timedelta(days=1)
                                continue
                        if grant_day not in existing_dates:
                            ref_schedule = (
                                helper.get_employee_day_schedule(emp, nearest_before, emp_tz)
                                if nearest_before else None
                            ) or (
                                helper.get_employee_day_schedule(emp, nearest_after, emp_tz)
                                if nearest_after else None
                            )
                            if ref_schedule:
                                sched_start = emp_tz.localize(
                                    dt(grant_day.year, grant_day.month, grant_day.day,
                                       ref_schedule['start'].hour,
                                       ref_schedule['start'].minute))
                                sched_end = emp_tz.localize(
                                    dt(grant_day.year, grant_day.month, grant_day.day,
                                       ref_schedule['end'].hour,
                                       ref_schedule['end'].minute))
                                # Night-shift fix: upstream discards the +1-day
                                # offset from get_employee_day_schedule, so
                                # sched_end ends up before sched_start.
                                if sched_end <= sched_start:
                                    sched_end += timedelta(days=1)
                                ci_utc = sched_start.astimezone(pytz.utc).replace(tzinfo=None)
                                co_utc = sched_end.astimezone(pytz.utc).replace(tzinfo=None)
                                break_hours = helper.calculate_break_deduction(
                                    emp, ref_day, sched_start, sched_end, emp_tz)
                                worked = ((co_utc - ci_utc).total_seconds() / 3600.0
                                          - break_hours)

                                rec = HrAttendance.sudo().create({
                                    'employee_id': emp.id,
                                    'check_in': ci_utc,
                                    'check_out': co_utc,
                                    'x_is_absent': False,
                                    'x_is_weekend': True,
                                    'x_weekend_granted': True,
                                })
                                rec.sudo().write({'worked_hours': worked})

                                existing_dates.add(grant_day)
                                total_created += 1
                                _logger.info(
                                    "Weekend granted for %s on %s (%.2fh)",
                                    emp.name, grant_day, worked)
                        grant_day += timedelta(days=1)

                current = weekend_end + timedelta(days=1)

            self.env.cr.commit()

    _DEVICE_PARAM = 'ksw_attendance_leave.generate_absences_device_id'

    @api.model
    def _run_generate_all_absences(self):
        """Override: scope the job to the device that triggered the button.

        The upstream implementation ignores which device the user clicked and
        processes every employee that has a biometric_user_id. That means a
        "Generate All Absences" on the main-office device also runs through
        workshop employees, causing:
          - spurious absence/weekend records for the wrong employees
          - cron timeout (all employees × full history > 120 s)

        Fix: biometric.device.details.action_generate_all_absences (see
        biometric_device_details.py) writes the triggering device's ID to an
        ir.config_parameter before kicking off the background cron.  We read
        that parameter here, filter to just that device's employees, and clear
        the parameter immediately so the next trigger is unblocked.

        If the parameter is absent (scheduled cron, shell call, etc.) we fall
        through to the upstream implementation so nothing changes for
        non-device-specific invocations.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        device_id_str = ICP.get_param(self._DEVICE_PARAM, default='')

        if not device_id_str:
            return super()._run_generate_all_absences()

        # Clear immediately so a second button press isn't blocked.
        ICP.set_param(self._DEVICE_PARAM, '')
        self.env.cr.commit()

        device_id = int(device_id_str)
        employees = self.env['hr.employee'].search([
            ('device_id', '=', device_id),
            ('biometric_user_id', '!=', False),
        ])
        if not employees:
            _logger.info(
                "Generate All Absences: no biometric employees for device %d",
                device_id)
            return True

        _logger.info(
            "Generate All Absences: device %d — %d employees",
            device_id, len(employees))

        today = fields.Date.context_today(self)
        yesterday = today - timedelta(days=1)
        total_created = 0

        for employee in employees:
            date_from = self._get_employee_start_date(employee)
            if date_from > yesterday:
                continue
            created = self._generate_absences_date_range(employee, date_from, yesterday)
            total_created += created

        earliest = min(
            (self._get_employee_start_date(e) for e in employees),
            default=yesterday,
        )
        self._generate_weekend_records(employees, earliest, yesterday)

        _logger.info(
            "Generate All Absences: device %d complete — %d absences created",
            device_id, total_created)
        return True

    @api.model
    def _check_absence_for_date(self, employee, check_date):
        """Override: create absence records even when a validated regular leave covers the date.

        Upstream skips the day when _has_leave_on_date() returns True, to avoid
        redundant records.  KSW needs those absence rows so
        hr.attendance._auto_link_regular_leave_coverage() can attach them to the
        validated leave, which then drives x_is_covered in the attendance view.

        We preserve the upstream skip ONLY for attendance-issue leave types
        (manually-linked late/early-leave records) — those already have real
        punch records selected by HR, so an extra absence row would be wrong.
        """
        sched_helper = self.env['biometric.schedule.helper']
        is_workday = (
            not sched_helper.is_calendar_configured(employee)
            or sched_helper.is_scheduled_workday(employee, check_date)
        )
        if not is_workday:
            return False
        if self._has_attendance_on_date(employee, check_date):
            return False
        if self._has_absence_on_date(employee, check_date):
            return False
        # Skip only when an attendance-issue leave (manual HR link) covers the date.
        # For regular leaves (sick, business trip, annual, etc.) we proceed so the
        # absence record is created and then auto-linked to the leave.
        att_issue_leave = self.env['hr.leave'].search([
            ('employee_id', '=', employee.id),
            ('state', '=', 'validate'),
            ('holiday_status_id.is_attendance_issue', '=', True),
            ('date_from', '<=', dt.combine(check_date, dt.max.time())),
            ('date_to', '>=', dt.combine(check_date, dt.min.time())),
        ], limit=1)
        if att_issue_leave:
            return False
        self._create_absence_record(employee, check_date)
        return True
