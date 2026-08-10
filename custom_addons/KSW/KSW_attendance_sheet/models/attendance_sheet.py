import logging
import pytz
from calendar import monthrange
from datetime import datetime as dt, timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Domain

_logger = logging.getLogger(__name__)


class KswAttendanceSheet(models.Model):
    _name = 'ksw.attendance.sheet'
    _description = 'Monthly Attendance Sheet'
    _inherit = ['mail.thread']
    _order = 'year desc, month desc, employee_id'

    employee_id = fields.Many2one(
        'hr.employee', string='Employee',
        required=True, ondelete='cascade',
        domain="[('x_is_attendance_sheet', '=', True)]",
    )
    month = fields.Selection([
        ('1', 'January'), ('2', 'February'), ('3', 'March'),
        ('4', 'April'), ('5', 'May'), ('6', 'June'),
        ('7', 'July'), ('8', 'August'), ('9', 'September'),
        ('10', 'October'), ('11', 'November'), ('12', 'December'),
    ], string='Month', required=True,
       default=lambda self: str(fields.Date.context_today(self).month))
    year = fields.Integer(
        string='Year', required=True,
        default=lambda self: fields.Date.context_today(self).year,
    )
    state = fields.Selection([
        ('draft', 'Draft'),
        ('confirmed', 'Confirmed'),
    ], string='Status', default='draft', copy=False)

    manager_id = fields.Many2one(
        'hr.employee', string='Manager',
        related='employee_id.parent_id', store=True,
    )
    department_id = fields.Many2one(
        'hr.department', string='Department',
        related='employee_id.department_id', store=True,
    )
    line_ids = fields.One2many(
        'ksw.attendance.sheet.line', 'sheet_id',
        string='Daily Attendance',
    )

    total_days = fields.Integer(
        string='Total Days', compute='_compute_totals', store=True,
    )
    total_attended = fields.Integer(
        string='Attended', compute='_compute_totals', store=True,
    )
    total_absent = fields.Integer(
        string='Absent', compute='_compute_totals', store=True,
    )
    is_locked = fields.Boolean(
        string='Locked', default=False,
        help='Locked sheets cannot be edited. '
             'Set automatically when the month ends.',
    )
    is_editable_period = fields.Boolean(
        string='Editable Period', compute='_compute_is_editable_period',
        help='False once the sheet is confirmed/locked, or — for '
             'supervisors — once it is no longer the current calendar '
             'month. Older months are kept open for review only.',
    )

    _unique_employee_month_year = models.Constraint(
        'UNIQUE(employee_id, month, year)',
        'Only one attendance sheet per employee per month is allowed.',
    )

    # ------------------------------------------------------------------
    # Auto-generate lines on creation
    # ------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        sheets = super().create(vals_list)
        sheets.action_generate_lines()
        return sheets

    def write(self, vals):
        # The period (employee/month/year) identifies which sheet this is
        # and must never be reassigned after creation — doing so used to
        # silently wipe the lines and repoint the record at a different
        # month, corrupting that month's chatter history. Wrong period?
        # Delete and recreate instead.
        if 'month' in vals or 'year' in vals:
            raise UserError(
                'The period of an attendance sheet cannot be changed. '
                'Delete this sheet and create a new one for the correct '
                'month instead.')
        return super().write(vals)

    def _check_editable(self):
        """Raise unless every sheet in self may have its lines edited."""
        is_manager = self.env.user.has_group(
            'KSW_attendance_sheet.group_attendance_sheet_manager')
        today = fields.Date.context_today(self)
        for sheet in self:
            if sheet.state == 'confirmed' or sheet.is_locked:
                raise UserError('Cannot modify a locked/confirmed sheet.')
            if not is_manager and (sheet.year, int(sheet.month)) != (
                    today.year, today.month):
                raise UserError(
                    'Supervisors can only edit the current month\'s '
                    'attendance. Older months are read-only for review.')

    # ------------------------------------------------------------------
    # Computed fields
    # ------------------------------------------------------------------

    @api.depends('line_ids.is_attended')
    def _compute_totals(self):
        for rec in self:
            all_lines = rec.line_ids
            rec.total_days = len(all_lines)
            rec.total_attended = len(all_lines.filtered('is_attended'))
            rec.total_absent = rec.total_days - rec.total_attended

    def _compute_display_name(self):
        month_names = dict(self._fields['month'].selection)
        for rec in self:
            emp = rec.employee_id.name or ''
            mn = month_names.get(rec.month, '')
            rec.display_name = f"{emp} - {mn} {rec.year}"

    @api.depends('state', 'is_locked', 'year', 'month')
    def _compute_is_editable_period(self):
        is_manager = self.env.user.has_group(
            'KSW_attendance_sheet.group_attendance_sheet_manager')
        today = fields.Date.context_today(self)
        for rec in self:
            rec.is_editable_period = (
                rec.state != 'confirmed' and not rec.is_locked
                and (is_manager or (rec.year, int(rec.month)) == (
                    today.year, today.month))
            )

    # ------------------------------------------------------------------
    # Schedule helpers
    # ------------------------------------------------------------------

    def _get_employee_tz(self, employee):
        """Return pytz timezone for an employee."""
        # sudo: schedule fields on hr.employee require hr.group_hr_user
        employee = employee.sudo()
        tz_name = (
            employee.resource_calendar_id.tz
            or employee.tz
            or employee.company_id.resource_calendar_id.tz
            or 'UTC'
        )
        return pytz.timezone(tz_name)

    def _get_employee_calendar(self, employee):
        """Resolve the calendar used for schedule lookups."""
        # sudo: main_calendar_id and other schedule fields on hr.employee
        # require hr.group_hr_user; sheet supervisors may not have that group.
        employee = employee.sudo()
        return (
            employee.main_calendar_id
            or employee.resource_calendar_id
            or employee.company_id.resource_calendar_id
        )

    def _is_misconfigured_calendar(self, employee):
        """True when the employee's calendar exists but defines no
        schedule at all (no calendar_group_ids and no attendance_ids).

        Used to distinguish "calendar selected but never given hours"
        (truly broken — must not default to attended) from "calendar
        properly configured, this particular day just isn't on it"
        (a genuine day off, e.g. Friday — see action_generate_lines).
        """
        calendar = self._get_employee_calendar(employee)
        return bool(calendar) and not calendar.calendar_group_ids \
            and not calendar.attendance_ids

    def _get_work_schedule(self, employee, check_date, preloaded_group_lines=None):
        """Get scheduled start/end for *employee* on *check_date*.

        Lookup order:
        1. calendar_group_ids → group lines  (KSW custom schedule)
        2. attendance_ids                    (standard Odoo schedule)
        3. Default Sun-Thu 08:00-17:00       (only if no calendar exists at all)

        A calendar that exists but has neither calendar_group_ids nor
        attendance_ids is treated as misconfigured, not as "no schedule
        restriction" — it must NOT silently grant a default workday. Doing
        so would let an incompletely-configured calendar (selected but
        never given hours) produce fully-paid attendance with no real
        schedule behind it.

        preloaded_group_lines: optional recordset of ALL resource.calendar.group.line
        records for this employee's calendar, pre-fetched by the caller for the whole
        month. When provided, filtering is done in Python instead of hitting the DB
        once per day (31 searches → 1 per employee).

        Returns dict(hour_from, hour_to, break_hours) or None.
        """
        calendar = self._get_employee_calendar(employee)
        employee = employee.sudo()

        day_of_week = str(check_date.weekday())

        # -- 1. Try calendar_group_ids (KSW custom groups) --
        if calendar and calendar.calendar_group_ids:
            if preloaded_group_lines is not None:
                all_lines = preloaded_group_lines.filtered(lambda l:
                    l.dayofweek == day_of_week
                    and (not l.start_date or l.start_date <= check_date)
                    and (not l.end_date or l.end_date >= check_date)
                ).sorted('hour_from')
            else:
                base_domain = Domain([
                    ('calendar_group_id', 'in', calendar.calendar_group_ids.ids),
                    ('dayofweek', '=', day_of_week),
                ])
                date_domain = Domain.AND([
                    Domain.OR([
                        Domain([('start_date', '=', False)]),
                        Domain([('start_date', '<=', check_date)]),
                    ]),
                    Domain.OR([
                        Domain([('end_date', '=', False)]),
                        Domain([('end_date', '>=', check_date)]),
                    ]),
                ])
                all_lines = self.env['resource.calendar.group.line'].search(
                    Domain.AND([base_domain, date_domain]),
                    order='hour_from asc',
                )
            if all_lines:
                work_lines = all_lines.filtered(
                    lambda l: l.day_period != 'break')
                break_lines = all_lines.filtered(
                    lambda l: l.day_period == 'break')
                if work_lines:
                    return {
                        'hour_from': work_lines[0].hour_from,
                        'hour_to': work_lines[-1].hour_to,
                        'break_hours': break_lines._duration_hours(),
                    }
            # Calendar has groups but this day is not scheduled → not a
            # workday, UNLESS this calendar forces Saturday to be required
            # (e.g. "Standard 40 hours/week" — see x_saturday_required).
            if calendar.x_saturday_required and day_of_week == '5':
                return {'hour_from': 8.0, 'hour_to': 17.0, 'break_hours': 1.0}
            return None

        # -- 2. Try standard Odoo attendance_ids --
        if calendar and calendar.attendance_ids:
            att_lines = calendar.attendance_ids.filtered(
                lambda a: a.dayofweek == day_of_week)
            if att_lines:
                work_atts = att_lines.filtered(
                    lambda a: a.day_period != 'break')
                break_atts = att_lines.filtered(
                    lambda a: a.day_period == 'break')
                if work_atts:
                    return {
                        'hour_from': work_atts[0].hour_from,
                        'hour_to': work_atts[-1].hour_to,
                        'break_hours': sum(
                            a.hour_to - a.hour_from for a in break_atts),
                    }
            # Calendar has attendance lines but this day is not scheduled,
            # UNLESS this calendar forces Saturday to be required.
            if calendar.x_saturday_required and day_of_week == '5':
                return {'hour_from': 8.0, 'hour_to': 17.0, 'break_hours': 1.0}
            return None

        # -- 3. Fallback: Sun-Thu 08:00-17:00 (Saudi standard) --
        # Only used when there is no calendar reference at all (employee,
        # employee's resource_calendar_id and company default are all
        # unset). A calendar that *exists* but has empty calendar_group_ids
        # and empty attendance_ids is a configuration error, not "no
        # restriction" — fail safe (no workday) instead of fail open.
        if calendar:
            _logger.warning(
                "Employee %s (id=%s): calendar '%s' has no "
                "calendar_group_ids and no attendance_ids configured. "
                "Treating %s as a non-workday until the calendar is fixed.",
                employee.name, employee.id, calendar.name, check_date,
            )
            return None

        # weekday(): Mon=0 … Sun=6  →  work days = Sun(6), Mon(0)-Thu(3)
        if check_date.weekday() in (0, 1, 2, 3, 6):
            return {
                'hour_from': 8.0,
                'hour_to': 17.0,
                'break_hours': 1.0,
            }

        return None

    def _is_workday(self, employee, check_date):
        """True when the employee has scheduled work on *check_date*."""
        return self._get_work_schedule(employee, check_date) is not None

    # ------------------------------------------------------------------
    # Attendance sync helpers
    # ------------------------------------------------------------------

    def _build_attendance_vals(self, employee, line_date, schedule):
        """Build check_in/check_out/worked_hours for an attended day."""
        emp_tz = self._get_employee_tz(employee)

        if schedule:
            hf = schedule['hour_from']
            ht = schedule['hour_to']
            brk = schedule.get('break_hours', 0.0)
            sh, sm = int(hf), int((hf % 1) * 60)
            eh, em = int(ht), int((ht % 1) * 60)
            local_ci = emp_tz.localize(dt(
                line_date.year, line_date.month, line_date.day, sh, sm,
            ))
            local_co = emp_tz.localize(dt(
                line_date.year, line_date.month, line_date.day, eh, em,
            ))
            if local_co <= local_ci:
                local_co += timedelta(days=1)
            ci_utc = local_ci.astimezone(pytz.utc).replace(tzinfo=None)
            co_utc = local_co.astimezone(pytz.utc).replace(tzinfo=None)
            worked = (co_utc - ci_utc).total_seconds() / 3600.0 - brk
        else:
            # Fallback: 08:00-17:00, 8 h
            local_ci = emp_tz.localize(dt(
                line_date.year, line_date.month, line_date.day, 8, 0,
            ))
            local_co = emp_tz.localize(dt(
                line_date.year, line_date.month, line_date.day, 17, 0,
            ))
            ci_utc = local_ci.astimezone(pytz.utc).replace(tzinfo=None)
            co_utc = local_co.astimezone(pytz.utc).replace(tzinfo=None)
            worked = 8.0

        return ci_utc, co_utc, max(0.0, worked)

    def _sync_line_attendance(self, lines, schedules=None):
        """Create or delete hr.attendance records to match is_attended.

        Called after lines are generated and whenever is_attended changes.
        Attendance creates are batched into a single ORM call to avoid
        per-record INSERT + _check_validity overhead on large sets.

        schedules: optional {date: schedule_dict} precomputed by
        action_generate_lines; avoids re-calling _get_work_schedule per line.
        """
        HrAttendance = self.env['hr.attendance'].sudo()

        create_vals = []
        create_lines = []
        create_worked = []

        for line in lines:
            employee = line.sheet_id.employee_id

            if line.is_attended and not line.attendance_id:
                # -- Check if a record already exists for this day --
                day_start = dt.combine(line.date, dt.min.time())
                day_end = dt.combine(
                    line.date + timedelta(days=1), dt.min.time(),
                )
                existing = HrAttendance.search([
                    ('employee_id', '=', employee.id),
                    ('check_in', '>=', day_start),
                    ('check_in', '<', day_end),
                ], limit=1)
                if existing:
                    line.sudo().write({'attendance_id': existing.id})
                    continue

                if not line.is_workday:
                    # Paid day off (e.g. Friday) with no real punch —
                    # nothing to link, and we must not fabricate a
                    # worked attendance record for a day off.
                    continue

                # -- Queue for batch create --
                if schedules is not None and line.date in schedules:
                    schedule = schedules[line.date]
                else:
                    schedule = line.sheet_id._get_work_schedule(
                        employee, line.date)
                ci_utc, co_utc, worked = self._build_attendance_vals(
                    employee, line.date, schedule)
                create_vals.append({
                    'employee_id': employee.id,
                    'check_in': ci_utc,
                    'check_out': co_utc,
                    'x_is_auto_generated': True,
                })
                create_lines.append(line)
                create_worked.append(worked)

            elif not line.is_attended and line.attendance_id:
                # -- Delete auto-generated record --
                if line.attendance_id.x_is_auto_generated:
                    line.attendance_id.sudo().unlink()
                line.sudo().write({'attendance_id': False})

        # -- Batch create all new attendance records in one INSERT --
        if create_vals:
            new_atts = HrAttendance.create(create_vals)
            for att, line, worked in zip(new_atts, create_lines, create_worked):
                att.write({'worked_hours': worked})
                line.sudo().write({'attendance_id': att.id})

    # ------------------------------------------------------------------
    # Weekly rest-day pay (e.g. Friday)
    # ------------------------------------------------------------------

    def _off_day_block(self, by_date, date):
        """Dates of the contiguous run of non-workdays containing *date*."""
        dates = {date}
        d = date - timedelta(days=1)
        while d in by_date and not by_date[d].is_workday:
            dates.add(d)
            d -= timedelta(days=1)
        d = date + timedelta(days=1)
        while d in by_date and not by_date[d].is_workday:
            dates.add(d)
            d += timedelta(days=1)
        return frozenset(dates)

    def _recompute_off_day_pay(self, changed_lines):
        """Re-derive pay for any off-day block bordered by a workday whose
        attendance just changed.

        An off day (e.g. Friday) defaults to paid (is_attended=True). It
        is only forfeited (is_attended=False) when the employee was
        absent on BOTH the workday immediately before AND immediately
        after the block — the standard weekly-rest-day rule. This is
        fully derived: it overwrites whatever the line currently holds,
        there is no separate manual-override state for off days.

        A block sitting on the first/last day of the month has only one
        neighbour inside this sheet — the other falls in an adjacent
        month whose sheet may not even exist yet (next month's sheet is
        only opened when that month starts). In that case the rule is
        decided on the neighbour we do have: absent before → the off day
        is forfeited. This month-edge fallback is specific to the manual
        attendance sheet; biometric absence generation
        (KSW_attendance_leave._generate_weekend_records) is unaffected
        and still requires a real workday on both sides.
        """
        for sheet in self:
            sheet_lines = sheet.line_ids
            by_date = {l.date: l for l in sheet_lines}
            changed_dates = changed_lines.filtered(
                lambda l: l.sheet_id == sheet).mapped('date')

            blocks = set()
            for d in changed_dates:
                for delta in (-1, 1):
                    neighbor = by_date.get(d + timedelta(days=delta))
                    if neighbor and not neighbor.is_workday:
                        blocks.add(self._off_day_block(by_date, neighbor.date))

            for block_dates in blocks:
                block = sheet_lines.filtered(lambda l: l.date in block_dates)
                before = by_date.get(min(block_dates) - timedelta(days=1))
                after = by_date.get(max(block_dates) + timedelta(days=1))
                # Only the neighbours that exist in this sheet count; a
                # block at the month edge is judged on its single known
                # side (see docstring). A block with no neighbour at all
                # (a whole month of off days) stays paid.
                known = [n for n in (before, after) if n]
                forfeited = bool(known) and not any(
                    n.is_attended for n in known)
                target = not forfeited
                to_update = block.filtered(
                    lambda l: l.is_attended != target
                )._filter_derivable_off_days()
                if to_update:
                    to_update.with_context(ksw_system_write=True).write(
                        {'is_attended': target})

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_generate_lines(self):
        """Create one line per calendar day of the month."""
        for sheet in self:
            if sheet.state == 'confirmed':
                raise UserError('Cannot generate lines for a confirmed sheet.')

            # Delete old lines (cascades to unlink auto-generated attendances
            # via ondelete='set null' + the unlink below).
            old_atts = sheet.line_ids.mapped('attendance_id').filtered(
                'x_is_auto_generated')
            sheet.line_ids.unlink()
            if old_atts.exists():
                old_atts.sudo().unlink()

            m = int(sheet.month)
            y = sheet.year
            num_days = monthrange(y, m)[1]
            misconfigured = self._is_misconfigured_calendar(sheet.employee_id)

            # Pre-load all group lines for this employee's calendar in one
            # query, then filter in Python per day — avoids 31 DB searches
            # per employee (one per day) during bulk sheet generation.
            calendar = self._get_employee_calendar(sheet.employee_id)
            preloaded_group_lines = None
            if calendar and calendar.calendar_group_ids:
                preloaded_group_lines = self.env[
                    'resource.calendar.group.line'].sudo().search([
                        ('calendar_group_id', 'in',
                         calendar.calendar_group_ids.ids),
                    ], order='hour_from asc')

            vals_list = []
            schedules = {}
            for day in range(1, num_days + 1):
                d = fields.Date.to_date(f'{y}-{m:02d}-{day:02d}')
                schedule = self._get_work_schedule(
                    sheet.employee_id, d,
                    preloaded_group_lines=preloaded_group_lines,
                )
                is_wd = schedule is not None
                schedules[d] = schedule
                vals_list.append({
                    'sheet_id': sheet.id,
                    'date': d,
                    'is_workday': is_wd,
                    # Workdays default to attended (HR flags exceptions).
                    # Non-workdays (e.g. Friday) also default to attended —
                    # they're a paid weekly rest day, not an absence —
                    # UNLESS the calendar is misconfigured (selected but
                    # never given hours), in which case every day looks
                    # like a non-workday and defaulting to attended would
                    # fabricate a full month of pay with no real schedule
                    # behind it.
                    'is_attended': is_wd or not misconfigured,
                })

            new_lines = self.env['ksw.attendance.sheet.line'].create(vals_list)
            # Immediately create hr.attendance records
            sheet._sync_line_attendance(new_lines, schedules=schedules)

    def action_mark_all_absent(self):
        """Set every workday to absent (off days are derived, not set)."""
        for sheet in self:
            sheet._check_editable()
            sheet.line_ids.filtered('is_workday').write({'is_attended': False})

    def action_mark_all_present(self):
        """Set every workday to present (off days are derived, not set)."""
        for sheet in self:
            sheet._check_editable()
            sheet.line_ids.filtered('is_workday').write({'is_attended': True})

    def _do_confirm(self):
        """Confirm sheets (internal).

        Called automatically by the monthly cron when the month ends.
        Attendance records already exist from real-time sync — only
        lines that are out of sync need a write; skipping in-sync lines
        makes bulk confirmation ~10x faster.
        """
        for sheet in self:
            if sheet.state == 'confirmed':
                continue
            if not sheet.line_ids:
                _logger.warning(
                    'Sheet %s has no lines, skipping confirmation.',
                    sheet.id,
                )
                continue

            # Only sync lines that are actually out of sync.
            out_of_sync = sheet.line_ids.filtered(
                lambda l: (l.is_attended and not l.attendance_id)
                or (not l.is_attended and l.attendance_id)
            )
            if out_of_sync:
                sheet._sync_line_attendance(out_of_sync)
            sheet.write({'state': 'confirmed', 'is_locked': True})

    def action_reset_to_draft(self):
        """Reset to draft and remove generated attendance records."""
        for sheet in self:
            if sheet.state != 'confirmed':
                raise UserError('Only confirmed sheets can be reset.')

            att_records = sheet.line_ids.mapped('attendance_id').filtered(
                'x_is_auto_generated',
            )
            sheet.line_ids.sudo().write({'attendance_id': False})
            if att_records:
                att_records.sudo().unlink()

            sheet.write({'state': 'draft', 'is_locked': False})
            # Re-sync attendance records for currently attended lines
            sheet._sync_line_attendance(sheet.line_ids)

    def action_generate_all_sheets(self):
        """Button wrapper so the list-header button can trigger the cron."""
        self._cron_generate_sheets()

    # ------------------------------------------------------------------
    # Cron
    # ------------------------------------------------------------------

    @api.model
    def _cron_generate_sheets(self):
        """Monthly cron: auto-confirm previous month sheets, then create
        new sheets for the current month.

        Each sheet/employee is wrapped in a savepoint so a single bad
        record cannot roll back the entire batch.

        Phase 1 (confirmations) is committed before Phase 2 (creation)
        begins so that June confirmations survive even if July creation
        fails or the cursor is closed mid-way (e.g. HTTP 120-s timeout).
        Phase 2 commits every 50 employees for the same reason.

        IMPORTANT: never access ORM fields inside an except block —
        the cursor may already be closed, making emp.name / sheet.x
        raise InterfaceError and escape the except, killing the whole job.
        Use emp.id (always in Python memory) instead.
        """
        today = fields.Date.context_today(self)

        # -- 1. Auto-confirm all draft sheets for previous months --
        draft_sheets = self.search([('state', '=', 'draft')])
        to_confirm = draft_sheets.filtered(
            lambda s: (s.year < today.year)
            or (s.year == today.year and int(s.month) < today.month)
        )
        confirmed = skipped = 0
        if to_confirm:
            _logger.info(
                'Attendance sheet cron: auto-confirming %d past-month sheets.',
                len(to_confirm),
            )
            for sheet in to_confirm:
                try:
                    with self.env.cr.savepoint():
                        sheet._do_confirm()
                    confirmed += 1
                except Exception:
                    skipped += 1
                    _logger.exception(
                        'Attendance sheet cron: failed to confirm sheet id=%s '
                        'month=%s year=%s — skipping.',
                        sheet.id, sheet.month, sheet.year,
                    )
            _logger.info(
                'Attendance sheet cron: confirmed %d, skipped %d.',
                confirmed, skipped,
            )

        # Commit Phase 1 before starting Phase 2 so confirmations are
        # permanently saved even if July sheet creation fails or the
        # cursor is killed (e.g. 120-s HTTP timeout on manual trigger).
        self.env.cr.commit()

        # -- 2. Generate sheets for the current month --
        month = str(today.month)
        year = today.year

        employees = self.env['hr.employee'].search([
            ('x_is_attendance_sheet', '=', True),
        ])
        existing = self.search([('month', '=', month), ('year', '=', year)])
        existing_emp_ids = set(existing.mapped('employee_id').ids)

        created = emp_skipped = 0
        for emp in employees:
            if emp.id not in existing_emp_ids:
                try:
                    with self.env.cr.savepoint():
                        self.create({
                            'employee_id': emp.id,
                            'month': month,
                            'year': year,
                        })
                    created += 1
                    # Commit every 50 employees so partial progress is
                    # saved if the cursor is killed mid-way.
                    if created % 50 == 0:
                        self.env.cr.commit()
                except Exception:
                    emp_skipped += 1
                    _logger.exception(
                        'Attendance sheet cron: failed to create sheet for '
                        'employee id=%s — skipping.',
                        emp.id,
                    )

        self.env.cr.commit()
        _logger.info(
            'Attendance sheet cron: created %d sheets for %s/%s '
            '(skipped %d employees).',
            created, month, year, emp_skipped,
        )




