# -*- coding: utf-8 -*-
import json
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

# How far back the daily cron re-checks weekend grants.  The grant decision
# for a given weekend depends on evidence that keeps arriving after the day
# itself (the next workday's punches, a leave approved days later), so the
# decision has to stay open for a while instead of being taken once.
_WEEKEND_RECHECK_LOOKBACK = 7


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

    # -- Targeted download: one period, chosen employees ----------------------

    @api.model
    def _fetch_attendance_range(self, device, date_from, date_to, employees):
        """Raw device logs for these employees inside this period.

        Unlike _fetch_biometric_attendance this applies no year filter, no
        batch cap and no incremental cutoff — the period *is* the filter — and
        it never touches ``last_download_time``.  Advancing that stamp here
        would make the incremental cron skip every punch between its previous
        cutoff and now, silently losing recent days just because someone
        re-pulled an old month.
        """
        bio_ids = {str(e.biometric_user_id)
                   for e in employees if e.biometric_user_id}
        if not bio_ids:
            _logger.warning(
                "Targeted download: none of the %d selected employees has a "
                "biometric ID.", len(employees))
            return []

        device_tz = pytz.timezone(device.tz or 'UTC')
        # Widen by a day on each side: _group_biometric_data files a
        # night-shift punch made before noon under the PREVIOUS day, so the
        # punches belonging to date_to arrive stamped date_to + 1.  The extra
        # days are trimmed back off after grouping.
        window_start = dt.combine(date_from - timedelta(days=1), dt.min.time())
        window_end = dt.combine(date_to + timedelta(days=2), dt.min.time())

        conn = device._connect_device()
        try:
            raw_logs = conn.get_attendance() or []
        finally:
            conn.disconnect()
        _logger.info(
            "Targeted download: %d raw logs on %s, filtering to %s..%s for "
            "%d biometric ID(s)",
            len(raw_logs), device.name, date_from, date_to, len(bio_ids))

        logs = []
        for log in raw_logs:
            if str(log.user_id) not in bio_ids:
                continue
            stamp = log.timestamp
            local = (device_tz.localize(stamp) if not stamp.tzinfo
                     else stamp.astimezone(device_tz))
            if not (window_start <= local.replace(tzinfo=None) < window_end):
                continue
            utc_time = local.astimezone(pytz.utc).replace(tzinfo=None)
            logs.append({
                'user_id': log.user_id,
                'timestamp': utc_time.strftime("%Y-%m-%d %H:%M:%S"),
            })
        return logs

    @api.model
    def download_attendance_range(self, device, date_from, date_to, employees):
        """Sync only the chosen employees' punches for the chosen period.

        Returns ``{'created': n, 'updated': n, 'days': n}``.
        """
        logs = self._fetch_attendance_range(
            device, date_from, date_to, employees)
        if not logs:
            return {'created': 0, 'updated': 0, 'days': 0}

        helper = self.env['biometric.schedule.helper']
        grouped = self._group_biometric_data(logs)
        allowed_ids = set(employees.ids)
        created = updated = days = 0

        for emp_id, day_map in grouped.items():
            if emp_id not in allowed_ids:
                continue
            employee = self.env['hr.employee'].browse(emp_id)
            emp_tz = helper.get_employee_tz(employee)
            for day, times in sorted(day_map.items()):
                # Drop the day we only widened the window to capture.
                if not (date_from <= day <= date_to):
                    continue
                was_created, was_updated = self._sync_attendance_record(
                    self.env.cr, self.env, emp_id, day, times, emp_tz)
                created += int(was_created)
                updated += int(was_updated)
                days += 1

        # _sync_attendance_record writes with raw SQL, so the ORM cache still
        # holds the pre-download picture. The absence and weekend passes that
        # follow read hr.attendance and would not see these rows.
        self.env.invalidate_all()

        _logger.info(
            "Targeted download complete: %d created, %d updated across %d "
            "employee-days", created, updated, days)
        return {'created': created, 'updated': updated, 'days': days}

    @api.model
    def _generate_absences_date_range(self, employees, date_from, date_to,
                                      commit=True):
        """Override: allow the per-employee commit to be switched off.

        Upstream commits (and rolls back) once per employee as a cron progress
        checkpoint. That is wrong inside an HTTP request — a later failure
        would leave the run half applied — and Odoo forbids it outright in
        tests. The cron path keeps the upstream behaviour untouched.
        """
        if commit:
            return super()._generate_absences_date_range(
                employees, date_from, date_to)

        total_created = 0
        for employee in employees:
            current_date = date_from
            while current_date <= date_to:
                if self._check_absence_for_date(employee, current_date):
                    total_created += 1
                current_date += timedelta(days=1)
        _logger.info(
            "Absence generation complete: %d created (no intermediate commit)",
            total_created)
        return total_created

    @api.model
    def _generate_weekend_records(self, employees, date_from, date_to,
                                  dry_run=False, commit=True):
        """Override: fix overnight-shift and late-sync issues.

        With ``dry_run=True`` nothing is written: the same decisions are made
        but grants are only counted, so the weekend-grant wizard can report
        what a real run would do.  Returns
        ``{'created': n, 'revoked': m}`` (upstream returns None and every
        upstream caller ignores the result).

        The per-employee ``commit`` is a cron progress checkpoint; callers
        running inside an HTTP request (the wizard) pass ``commit=False`` so a
        later failure rolls the whole run back instead of leaving it half
        applied.  Odoo also forbids committing from inside a test.

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
        total_revoked = 0

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
                        total_revoked += len(stale)
                        if not dry_run:
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
                                    total_revoked += len(stale)
                                    if not dry_run:
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

                                if not dry_run:
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
                                    "Weekend %s for %s on %s (%.2fh)",
                                    'missing' if dry_run else 'granted',
                                    emp.name, grant_day, worked)
                        grant_day += timedelta(days=1)

                current = weekend_end + timedelta(days=1)

            if not dry_run and commit:
                self.env.cr.commit()

        return {'created': total_created, 'revoked': total_revoked}

    # ------------------------------------------------------------------
    # Re-evaluation after a time-off decision
    # ------------------------------------------------------------------

    @api.model
    def _regenerate_weekends_for_leaves(self, leaves):
        """Re-run the weekend grant around the dates of `leaves`.

        The weekend grant is decided from a *snapshot*: the daily cron looks
        at a one-day window and asks whether the immediately adjacent workday
        is attended.  A validated leave turns that day's absence into a
        covered absence, which counts as attended — but the leave is almost
        always approved *after* the cron already made (and never revisits)
        its decision, so the Friday is silently lost forever.

        Approving/refusing a leave therefore has to re-open the decision for
        the days around it.  `_generate_weekend_records` is idempotent: it
        skips days that already have a record and revokes stale grants whose
        adjacent workdays are no longer attended, so this equally handles the
        refuse/reset direction.

        Days in the future are excluded — a weekend is only ever granted once
        the surrounding days have actually happened, matching the cron.
        """
        leaves = leaves.filtered(
            lambda l: l.employee_id and l.request_date_from and l.request_date_to
        )
        if not leaves:
            return {'created': 0, 'revoked': 0}

        # This is a system bookkeeping pass triggered by an already-authorised
        # action (leave approve/refuse/draft), not a user-facing read. Run it
        # sudo'd throughout: the chain below (main_calendar_id,
        # biometric_user_id, and anything get_employee_day_schedule reads) is
        # all groups='hr.group_hr_user'-gated, and the caller is frequently a
        # Direct Manager who lacks that group.
        leaves = leaves.sudo()

        yesterday = fields.Date.context_today(self) - timedelta(days=1)
        created = revoked = 0

        for employee in leaves.mapped('employee_id'):
            if not employee.biometric_user_id:
                continue
            emp_leaves = leaves.filtered(lambda l: l.employee_id == employee)
            date_from = (min(emp_leaves.mapped('request_date_from'))
                         - timedelta(days=_ADJACENT_WINDOW))
            date_to = (max(emp_leaves.mapped('request_date_to'))
                       + timedelta(days=_ADJACENT_WINDOW))
            date_to = min(date_to, yesterday)
            if date_from > date_to:
                continue
            # commit=False: this runs inside the approval request, so a later
            # failure must roll the whole thing back.
            result = self._generate_weekend_records(
                employee, date_from, date_to, commit=False)
            created += result['created']
            revoked += result['revoked']

        if created or revoked:
            _logger.info(
                "Weekend re-check after time-off decision: %d granted, "
                "%d revoked across %d leave(s)",
                created, revoked, len(leaves))
        return {'created': created, 'revoked': revoked}

    @api.model
    def cron_generate_absences(self):
        """Override: give recent weekends a second look.

        Upstream evaluates only `lastcall .. yesterday`, i.e. a single day.
        That day is judged on whether the immediately adjacent workday is
        attended — and on the morning after a Friday neither side is settled
        yet: Thursday's absence may not be covered (the leave gets approved
        days later) and Saturday's punches have not been downloaded at all.
        The Friday is then skipped permanently, because no later run ever
        looks at it again.

        A second, idempotent weekend pass over the preceding
        _WEEKEND_RECHECK_LOOKBACK days lets those decisions settle: grants
        appear once the evidence arrives and stale ones are revoked.
        """
        res = super().cron_generate_absences()

        yesterday = fields.Date.context_today(self) - timedelta(days=1)
        date_from = yesterday - timedelta(days=_WEEKEND_RECHECK_LOOKBACK)
        employees = self.env['hr.employee'].search([
            ('biometric_user_id', '!=', False),
        ])
        if employees:
            result = self._generate_weekend_records(
                employees, date_from, yesterday)
            _logger.info(
                "Weekend re-check %s to %s: %d granted, %d revoked",
                date_from, yesterday, result['created'], result['revoked'])
        return res

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

    _WEEKEND_PARAM = 'ksw_attendance_leave.weekend_grant_job'

    @api.model
    def _run_generate_weekend_grants(self):
        """Cron entry point for the "Generate Weekend Grants" wizard.

        Weekend re-generation on its own (no absence scan) is what's needed
        after attendance has been deleted and re-downloaded: the download
        restores the punches but not the granted Friday/Saturday records,
        which only ever exist as Odoo-side rows.

        The wizard writes its parameters as JSON to an ir.config_parameter
        and triggers this cron; we read and clear them immediately so a
        second run isn't blocked.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        raw = ICP.get_param(self._WEEKEND_PARAM, default='')
        # The wizard re-enables the cron for each run; park it again so it
        # never fires on its own schedule.
        cron = self.env.ref(
            'KSW_attendance_leave.ir_cron_ksw_generate_weekend_grants',
            raise_if_not_found=False)
        if cron:
            cron.sudo().write({'active': False})

        if not raw:
            _logger.warning(
                "Generate Weekend Grants: no job parameters found, nothing to do.")
            return True

        ICP.set_param(self._WEEKEND_PARAM, '')
        self.env.cr.commit()

        job = json.loads(raw)
        employees = self.env['hr.employee'].browse(job['employee_ids']).exists()
        date_from = fields.Date.to_date(job['date_from'])
        date_to = fields.Date.to_date(job['date_to'])
        if not employees:
            _logger.info("Generate Weekend Grants: no employees in job, skipping.")
            return True

        _logger.info(
            "Generate Weekend Grants: %d employees, %s to %s",
            len(employees), date_from, date_to)
        result = self._generate_weekend_records(employees, date_from, date_to)
        _logger.info(
            "Generate Weekend Grants complete: %d granted, %d revoked",
            result['created'], result['revoked'])
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
