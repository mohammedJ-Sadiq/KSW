import logging
import pytz
from calendar import monthrange
from datetime import date, datetime as dt, timedelta
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Domain

_logger = logging.getLogger(__name__)


class KswAttendanceSheet(models.Model):
    _name = 'ksw.attendance.sheet'
    _description = 'Monthly Attendance Sheet'
    _inherit = ['mail.thread']
    # Blocked sheets first: they are the ones somebody has to do something
    # about, and burying them in a month-ordered list is how a whole team
    # reaches payroll unconfirmed.
    _order = 'x_is_blocked desc, year desc, month desc, employee_id'

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

    x_confirmed_by = fields.Many2one(
        'res.users', string='Confirmed By',
        readonly=True, copy=False,
        help='The supervisor who released this month to payroll.',
    )
    x_confirmed_on = fields.Datetime(
        string='Confirmed On', readonly=True, copy=False,
    )
    x_can_confirm = fields.Boolean(
        string='Can Confirm', compute='_compute_x_can_confirm',
        help='True when the current user may release this sheet to '
             'payroll. Drives the Confirm button; the real gate is the '
             'server-side check in action_supervisor_confirm.',
    )

    # ── Blocked state ────────────────────────────────────────────────
    # Stored so the list can sort blocked sheets to the top and filter on
    # them. DISPLAY ONLY — never the gate. action_supervisor_confirm always
    # re-evaluates _confirmation_blockers() live, so a stale flag can
    # neither let a bad confirmation through nor refuse a good one. That is
    # what makes it safe to keep these fresh with explicit triggers rather
    # than a depends= chain reaching into hr.leave.
    # default=False matters for more than tidiness: _order sorts this
    # DESC, and Postgres puts NULLs *first* on a DESC sort — so a row with
    # an unset flag outranks a genuinely blocked one and lands at the top
    # of the list it is supposed to be absent from.
    x_is_blocked = fields.Boolean(
        string='Needs Attention', readonly=True, index=True, copy=False,
        default=False,
        help='This month cannot be sent to payroll yet. See Blocked Reason.',
    )
    x_blocked_reason = fields.Text(
        string='Blocked Reason', readonly=True, copy=False,
    )
    x_action_owner_id = fields.Many2one(
        'res.users', string='Waiting On', readonly=True, index=True,
        copy=False,
        help='The person who has to act next before this month can be sent '
             'to payroll.',
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
        sheets._recompute_blocked()
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
    # Confirmation authority
    # ------------------------------------------------------------------

    def _is_sheet_administrator(self, user):
        """True for the roles that may act on a sheet they do not manage.

        Deliberately NOT `hr.group_hr_user`. That group is held by 14 people
        in this database, several of them line supervisors with 56 and 90
        direct reports — counting them as administrators let a supervisor
        confirm any other team's month, and made them land on a
        "here is everyone's work" screen instead of their own. Full access
        belongs to the dedicated Attendance Sheet Manager group, whose
        membership is the actual decision someone made.
        """
        return user.has_group(
            'KSW_attendance_sheet.group_attendance_sheet_manager')

    def _authority_denial_reason(self, user):
        """Why *user* has no say over this sheet — '' when they do.

        Authority only: who the person is and whether the month is still
        theirs. Kept separate from the state checks in
        `_confirm_denial_reason` so that actions which exist precisely to
        clear a blocked state (see action_apply_approved_leave) can ask
        "may this person touch this sheet?" without also being told
        "…but it is blocked", which is the thing they are there to fix.
        """
        self.ensure_one()
        # sudo: manager_id.user_id is an identity read. A supervisor has no
        # model-level access to hr.employee, so resolving their own manager
        # link must not depend on HR rights.
        manager_user = self.sudo().manager_id.user_id
        is_admin = self._is_sheet_administrator(user)
        if not is_admin and manager_user != user:
            return _(
                'Only %(manager)s (the direct manager) can act on this '
                'attendance sheet.',
                manager=(self.sudo().manager_id.name
                         or _('the direct manager')),
            )

        today = fields.Date.context_today(self)
        if not is_admin and (self.year, int(self.month)) != (
                today.year, today.month):
            return _(
                'Supervisors can only act on the current month. %(period)s '
                'has already closed — ask HR to handle it for you.',
                period=self.display_name,
            )
        return ''

    def _confirm_denial_reason(self, user):
        """Why *user* may not confirm this sheet — '' when they may.

        One predicate, read by both the button-visibility compute and the
        server-side guard, so the button can never offer an action the
        guard refuses (and vice versa).
        """
        self.ensure_one()
        if self.state == 'confirmed':
            return _('This sheet is already confirmed.')
        if not self.line_ids:
            return _('This sheet has no daily attendance lines yet.')
        return self._authority_denial_reason(user)

    @api.depends_context('uid')
    @api.depends('state', 'line_ids', 'manager_id', 'month', 'year')
    def _compute_x_can_confirm(self):
        user = self.env.user
        for rec in self:
            rec.x_can_confirm = not rec._confirm_denial_reason(user)

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

    # ------------------------------------------------------------------
    # Confirmation blockers — the sheet must agree with the leave record
    # ------------------------------------------------------------------

    def _period_bounds(self):
        """First and last calendar date of this sheet's month."""
        self.ensure_one()
        m, y = int(self.month), self.year
        return date(y, m, 1), date(y, m, monthrange(y, m)[1])

    def _confirmation_blockers(self):
        """Reasons this sheet contradicts the approved leave record.

        Returns a list of human-readable strings; empty means the sheet may
        be released to payroll. Dependent modules extend this — KSW_payroll
        adds the unresolved vacation-return rule — so every new consistency
        rule lands in one place and is enforced by every confirm route.
        """
        self.ensure_one()
        blockers = []

        date_from, date_to = self._period_bounds()
        leaves = self.env['hr.leave'].sudo().search([
            ('employee_id', '=', self.employee_id.id),
            ('state', '=', 'validate'),
            ('request_date_from', '<=', date_to),
            ('request_date_to', '>=', date_from),
        ])
        if not leaves:
            return blockers

        for leave in leaves:
            end = self._leave_coverage_end(leave, date_to)
            clashing = sorted(
                line.date for line in self.line_ids
                if line.is_attended
                and leave.request_date_from <= line.date <= end
            )
            if not clashing:
                continue
            open_ended = end != (leave.request_date_to
                                 or leave.request_date_from)
            blockers.append(_(
                '%(count)s day(s) are marked Attended while %(type)s '
                '(%(from_)s → %(to_)s) is approved: %(days)s%(note)s',
                count=len(clashing),
                type=leave.holiday_status_id.name,
                from_=leave.request_date_from,
                to_=leave.request_date_to,
                days=', '.join(d.strftime('%d %b') for d in clashing),
                note=(_('\n      The return was never confirmed, so this '
                        'leave is treated as still running to the end of '
                        'the month — there is no evidence the employee came '
                        'back on %(planned)s.',
                        planned=leave.request_date_to)
                      if open_ended else ''),
            ))
        return blockers

    def _leave_coverage_end(self, leave, period_end):
        """Last date this sheet should treat as covered by *leave*.

        Normally the requested end date. The hook exists because a leave
        whose return nobody has confirmed does not actually end when it
        said it would — KSW_payroll extends it to the end of the period,
        because until someone confirms the return there is no evidence the
        employee came back at all.
        """
        self.ensure_one()
        return leave.request_date_to or leave.request_date_from

    def _covered_line_ids(self, leaves, period_end):
        """Line ids this sheet should record as absent for these leaves."""
        self.ensure_one()
        covered = set()
        for leave in leaves:
            start = leave.request_date_from
            end = self._leave_coverage_end(leave, period_end)
            if not start or not end:
                continue
            covered.update(
                line.id for line in self.line_ids
                if start <= line.date <= end
            )
        return covered

    def _confirmation_owner(self):
        """The user who has to act next before this sheet can be released.

        Default: the sheet's own manager (`manager_id`, i.e.
        `employee_id.parent_id`) — they are the one who presses Confirm.
        Dependent modules override when a *different* person holds the
        blocker; KSW_payroll returns the leave manager when a vacation
        return is unconfirmed, because `parent_id` and `leave_manager_id`
        are not the same field and do diverge in this database.
        """
        self.ensure_one()
        return self.sudo().manager_id.user_id

    def _recompute_blocked(self):
        """Refresh the display-only blocked flags from the live blockers.

        Cheap enough to call from every write path that can change the
        answer. Writes only when something actually changed, so it does not
        churn the chatter or the write_date.
        """
        for sheet in self:
            if sheet.state == 'confirmed':
                vals = {
                    'x_is_blocked': False,
                    'x_blocked_reason': False,
                    'x_action_owner_id': False,
                }
            else:
                blockers = sheet._confirmation_blockers()
                owner = sheet._confirmation_owner()
                vals = {
                    'x_is_blocked': bool(blockers),
                    'x_blocked_reason': '\n'.join(blockers) or False,
                    'x_action_owner_id': owner.id if owner else False,
                }
            current = {
                'x_is_blocked': sheet.x_is_blocked,
                'x_blocked_reason': sheet.x_blocked_reason or False,
                'x_action_owner_id': sheet.x_action_owner_id.id or False,
            }
            if current != vals:
                sheet.sudo().write(vals)

    @api.model
    def _recompute_blocked_for_employees(self, employee_ids, date_from=None,
                                         date_to=None):
        """Refresh every draft sheet of these employees covering the range.

        Called from the leave side: approving, refusing or confirming the
        return of a leave changes whether a sheet is blocked, and none of
        those touch the sheet's own fields.
        """
        if not employee_ids:
            return
        domain = [
            ('employee_id', 'in', list(employee_ids)),
            ('state', '=', 'draft'),
        ]
        sheets = self.sudo().search(domain)
        if date_from and date_to:
            sheets = sheets.filtered(lambda s: not (
                s._period_bounds()[1] < date_from
                or s._period_bounds()[0] > date_to))
        sheets._recompute_blocked()

    def _check_confirmation_blockers(self):
        """Raise listing every contradiction across the sheets in self."""
        problems = []
        for sheet in self:
            blockers = sheet._confirmation_blockers()
            if blockers:
                problems.append('%s:\n%s' % (
                    sheet.display_name,
                    '\n'.join('  • %s' % b for b in blockers),
                ))
        if problems:
            raise UserError(_(
                'This attendance cannot be sent to payroll — it '
                'contradicts approved time off.\n\n%(problems)s\n\n'
                'Correct the days above (or the time-off request), then '
                'confirm again.',
                problems='\n\n'.join(problems),
            ))

    # ------------------------------------------------------------------
    # Supervisor confirmation
    # ------------------------------------------------------------------

    def action_supervisor_confirm(self):
        """Release this month's attendance to payroll.

        Until this runs, the payslip batch reads the month as zero
        attendance — the sheet is a supervisor's assertion, and nothing
        should be paid on an assertion nobody made.
        """
        user = self.env.user
        if not self.env.su:
            for sheet in self:
                reason = sheet._confirm_denial_reason(user)
                if reason:
                    raise UserError(reason)
        self._check_confirmation_blockers()

        for sheet in self:
            sheet._do_confirm()
            sheet.write({
                'x_confirmed_by': user.id,
                'x_confirmed_on': fields.Datetime.now(),
            })
            sheet._recompute_blocked()
            sheet.message_post(
                body=Markup(
                    '<strong>✅ Attendance Sent to Payroll</strong><br/>'
                    '<b>Confirmed by:</b> %(user)s<br/>'
                    '<b>Attended:</b> %(attended)s day(s)<br/>'
                    '<b>Absent:</b> %(absent)s day(s)<br/>'
                    '<i>The payslip batch will now use these figures.</i>'
                ) % {
                    'user': user.name,
                    'attended': sheet.total_attended,
                    'absent': sheet.total_absent,
                },
            )
        return True

    def action_apply_approved_leave(self):
        """Mark every day that clashes with an approved leave as absent.

        These clashes are not supervisor mistakes. A sheet is generated at
        the start of its month, so a vacation approved *earlier* — the
        common shape here is one running from May or June into August —
        was locked onto the months that existed at approval time and never
        reached this one. The supervisor is then asked to fix 16 to 31 days
        by hand before they can confirm, which is how a gate turns into
        something people route around.

        The leave record is the authority on those days, so applying it is
        a transcription, not a decision.
        """
        user = self.env.user
        if not self.env.su:
            for sheet in self:
                if sheet.state == 'confirmed':
                    raise UserError(_(
                        'This sheet is already confirmed. Reset it to draft '
                        'first if the days need correcting.'))
                reason = sheet._authority_denial_reason(user)
                if reason:
                    raise UserError(reason)

        for sheet in self:
            date_from, date_to = sheet._period_bounds()
            leaves = self.env['hr.leave'].sudo().search([
                ('employee_id', '=', sheet.employee_id.id),
                ('state', '=', 'validate'),
                ('request_date_from', '<=', date_to),
                ('request_date_to', '>=', date_from),
            ])
            if not leaves:
                continue

            covered = sheet._covered_line_ids(leaves, date_to)
            to_mark = sheet.line_ids.filtered(
                lambda l: l.id in covered and l.is_attended)
            if not to_mark:
                continue

            dates = sorted(to_mark.mapped('date'))
            to_mark.with_context(ksw_system_write=True).write(
                {'is_attended': False})
            sheet.sudo().message_post(
                body=Markup(
                    '<strong>📋 Approved Time Off Applied</strong><br/>'
                    '<b>%(count)s day(s)</b> marked absent to match the '
                    'approved time off: %(days)s<br/>'
                    '<i>Applied by %(user)s.</i>'
                ) % {
                    'count': len(dates),
                    'days': ', '.join(d.strftime('%d %b') for d in dates),
                    'user': user.name,
                },
                subtype_xmlid='mail.mt_note',
            )
        self._recompute_blocked()
        return True

    def _current_month_drafts(self):
        today = fields.Date.context_today(self)
        return self.sudo().search([
            ('state', '=', 'draft'),
            ('month', '=', str(today.month)),
            ('year', '=', today.year),
        ])

    def _my_pending_sheets(self):
        """Current-month drafts to act on when nothing is selected.

        An administrator is deliberately NOT given the whole database here.
        They can confirm any sheet, but "confirm 363 employees' months
        because I clicked a header button" is not an intent anyone has —
        they get the diagnosis instead and pick rows explicitly.
        """
        if self._is_sheet_administrator(self.env.user):
            return self.browse()
        today = fields.Date.context_today(self)
        return self.search([
            ('state', '=', 'draft'),
            ('month', '=', str(today.month)),
            ('year', '=', today.year),
            ('manager_id.user_id', '=', self.env.uid),
        ])

    def _no_pending_sheets_message(self):
        """Say why there is nothing to confirm — and who is holding what.

        "You have no unconfirmed attendance sheets for this month" is true
        and useless: it does not distinguish an administrator who manages
        nobody from a supervisor whose team is already done, and it never
        names the person actually sitting on the work.
        """
        today = fields.Date.context_today(self)
        month_label = dict(self._fields['month'].selection).get(
            str(today.month), '')
        period = '%s %s' % (month_label, today.year)
        drafts = self._current_month_drafts()

        if self._is_sheet_administrator(self.env.user):
            if not drafts:
                return _(
                    'Every attendance sheet for %(period)s has already been '
                    'sent to payroll — there is nothing left to confirm.',
                    period=period)
            by_manager = {}
            for sheet in drafts:
                name = sheet.manager_id.name or _('(no direct manager)')
                by_manager[name] = by_manager.get(name, 0) + 1
            listing = '\n'.join(
                '  • %s — %s employee(s)' % (name, count)
                for name, count in sorted(
                    by_manager.items(), key=lambda kv: -kv[1])
            )
            return _(
                'Nothing was selected, and you are not the direct manager of '
                'any employee, so there is no team to confirm on your own '
                'behalf.\n\n'
                '%(period)s still has %(count)s unconfirmed sheet(s), held '
                'by:\n%(listing)s\n\n'
                'As an administrator you can confirm any of them — tick the '
                'rows you want in the list first, then press the button '
                'again.',
                period=period, count=len(drafts), listing=listing)

        # Ordinary supervisor.
        mine_any_state = self.search_count([
            ('month', '=', str(today.month)),
            ('year', '=', today.year),
            ('manager_id.user_id', '=', self.env.uid),
        ])
        if not mine_any_state:
            return _(
                'You have no attendance sheets for %(period)s.\n\n'
                'Attendance sheets are only created for employees marked '
                '"Uses Attendance Sheet" whose Manager is you. If someone '
                'should be on your list, ask HR to check that employee\'s '
                'Manager field.',
                period=period)
        return _(
            'All %(count)s of your attendance sheets for %(period)s have '
            'already been sent to payroll. There is nothing left to '
            'confirm.',
            count=mine_any_state, period=period)

    def action_confirm_my_team(self):
        """Bulk confirm from the list header.

        Reports every problem at once rather than stopping on the first —
        a supervisor confirming a team of twenty should not have to
        discover their contradictions one refusal at a time.
        """
        sheets = self
        if not sheets:
            sheets = self._my_pending_sheets()
        if not sheets:
            raise UserError(self._no_pending_sheets_message())

        user = self.env.user
        confirmed = self.browse()
        problems = []
        for sheet in sheets:
            reason = (
                '' if self.env.su else sheet._confirm_denial_reason(user))
            blockers = sheet._confirmation_blockers() if not reason else []
            if reason or blockers:
                problems.append('%s:\n%s' % (
                    sheet.display_name,
                    '\n'.join('  • %s' % b for b in ([reason] if reason
                                                     else blockers)),
                ))
                continue
            confirmed |= sheet

        detail = '\n\n'.join(problems)
        if not confirmed:
            # Nothing succeeded, so there is nothing for the rollback to
            # undo — a hard error is the clearest thing to show.
            raise UserError(_(
                'None of the selected attendance sheets could be sent to '
                'payroll:\n\n%(problems)s', problems=detail))

        confirmed.sudo().action_supervisor_confirm()

        if problems:
            # A UserError here would roll back the confirmations above and
            # contradict its own message — report instead, and keep them.
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _(
                        '%(done)s Sent to Payroll — %(failed)s Refused',
                        done=len(confirmed), failed=len(problems),
                    ),
                    'message': _(
                        'These sheets still contradict approved time off '
                        'and were left unconfirmed:\n\n%(problems)s',
                        problems=detail,
                    ),
                    'type': 'warning',
                    'sticky': True,
                    'next': {'type': 'ir.actions.act_window_close'},
                },
            }
        return True

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

    def _reopen_for_change(self, reason):
        """Send a confirmed sheet back to draft because its days changed.

        Called when an approved leave (or any other system write) alters
        attendance the supervisor already released to payroll. The confirmed
        figures no longer describe the month, so the confirmation is
        withdrawn and the supervisor is asked to look again.

        Deliberately NOT action_reset_to_draft(): that one deletes and
        rebuilds the hr.attendance records, which would throw away the very
        change that triggered this.
        """
        for sheet in self:
            if sheet.state != 'confirmed':
                continue
            confirmer = sheet.x_confirmed_by
            sheet.sudo().write({
                'state': 'draft',
                'is_locked': False,
                'x_confirmed_by': False,
                'x_confirmed_on': False,
            })
            sheet.sudo().message_post(
                body=Markup(
                    '<strong>↩ Confirmation Withdrawn</strong><br/>'
                    '<b>Reason:</b> %(reason)s<br/>'
                    '<i>This month was already sent to payroll. The figures '
                    'have changed, so it is back in Draft and must be '
                    'confirmed again — until then payroll reads it as zero '
                    'attendance.</i>'
                ) % {'reason': reason},
                subtype_xmlid='mail.mt_note',
            )
            sheet._recompute_blocked()
            manager_user = sheet.sudo().manager_id.user_id or confirmer
            if manager_user:
                self._notify_partners(
                    manager_user.partner_id,
                    _('Re-confirm attendance for %(employee)s',
                      employee=sheet.employee_id.name or ''),
                    Markup(
                        '<strong>↩ %(period)s needs confirming again'
                        '</strong><br/>%(reason)s<br/>'
                        '<i>Open the sheet, check the days, then press '
                        '<b>Confirm &amp; Send to Payroll</b>. Until you do, '
                        'this month is paid as zero attendance.</i>'
                    ) % {
                        'period': sheet.display_name,
                        'reason': reason,
                    },
                )

    def action_generate_all_sheets(self):
        """Button wrapper so the list-header button can trigger the cron."""
        self._cron_generate_sheets()

    # ------------------------------------------------------------------
    # Cron
    # ------------------------------------------------------------------

    @api.model
    def _cron_generate_sheets(self, commit=True):
        """Monthly cron: close out last month, then create this month's
        sheets.

        ``commit`` is the cron progress checkpoint; tests pass False
        (Odoo forbids committing from inside a test).

        Each sheet/employee is wrapped in a savepoint so a single bad
        record cannot roll back the entire batch.

        Phase 1 (closing) is committed before Phase 2 (creation) begins so
        that June's closing notes survive even if July creation fails or the
        cursor is closed mid-way (e.g. HTTP 120-s timeout). Phase 2 commits
        every 50 employees for the same reason.

        IMPORTANT: never access ORM fields inside an except block —
        the cursor may already be closed, making emp.name / sheet.x
        raise InterfaceError and escape the except, killing the whole job.
        Use emp.id (always in Python memory) instead.
        """
        today = fields.Date.context_today(self)

        # -- 1. Close out past-month sheets the supervisor never confirmed --
        # This used to auto-confirm them, which meant a supervisor who never
        # opened the sheet still produced a full month of paid attendance
        # signed by nobody. They now stay draft: payroll reads an unconfirmed
        # month as zero attendance (KSW_payroll._worked_day_lines_sheet), and
        # only HR / an Attendance Sheet Manager can still confirm one late.
        draft_sheets = self.search([('state', '=', 'draft')])
        unconfirmed = draft_sheets.filtered(
            lambda s: (s.year < today.year)
            or (s.year == today.year and int(s.month) < today.month)
        )
        noted = skipped = 0
        if unconfirmed:
            _logger.info(
                'Attendance sheet cron: %d past-month sheets closed without '
                'supervisor confirmation.',
                len(unconfirmed),
            )
            for sheet in unconfirmed:
                try:
                    with self.env.cr.savepoint():
                        sheet._post_unconfirmed_close_note()
                    noted += 1
                except Exception:
                    skipped += 1
                    # sheet.id only — reading month/year here can raise
                    # InterfaceError on a closed cursor (see docstring).
                    _logger.exception(
                        'Attendance sheet cron: failed to flag unconfirmed '
                        'sheet id=%s — skipping.', sheet.id,
                    )
            _logger.info(
                'Attendance sheet cron: flagged %d unconfirmed, skipped %d.',
                noted, skipped,
            )

        # Commit Phase 1 before starting Phase 2 so the closing notes are
        # permanently saved even if July sheet creation fails or the
        # cursor is killed (e.g. 120-s HTTP timeout on manual trigger).
        if commit:
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
                    if commit and created % 50 == 0:
                        self.env.cr.commit()
                except Exception:
                    emp_skipped += 1
                    _logger.exception(
                        'Attendance sheet cron: failed to create sheet for '
                        'employee id=%s — skipping.',
                        emp.id,
                    )

        if commit:
            self.env.cr.commit()
        _logger.info(
            'Attendance sheet cron: created %d sheets for %s/%s '
            '(skipped %d employees).',
            created, month, year, emp_skipped,
        )

    # ------------------------------------------------------------------
    # Month-end reminder
    # ------------------------------------------------------------------

    def _post_unconfirmed_close_note(self):
        """Record on the sheet that its month closed unconfirmed."""
        self.ensure_one()
        self.message_post(
            body=Markup(
                '<strong>⚠️ Month Closed Without Confirmation</strong><br/>'
                '%(period)s ended without the direct manager sending this '
                'attendance to payroll.<br/>'
                '<i>Payroll will read this month as <b>zero attendance</b> '
                'until it is confirmed. Ask HR to confirm it if the '
                'attendance above is correct.</i>'
            ) % {'period': self.display_name},
            subtype_xmlid='mail.mt_note',
        )

    def _notify_partners(self, partners, subject, body):
        """Send an inbox/email notification not tied to one document."""
        if not partners:
            return
        self.env['mail.thread'].message_notify(
            partner_ids=partners.ids, subject=subject, body=body,
        )

    @api.model
    def _cron_month_end_confirmation_reminder(self, force=False):
        """On the last day of the month, chase every supervisor still
        holding unconfirmed sheets.

        Runs daily and returns immediately on any other day — the reminder
        has to land while the supervisor can still act on it, and a
        supervisor may only confirm during the sheet's own month.

        ``force`` is for tests and manual shell runs; it skips the
        last-day-of-month check.
        """
        today = fields.Date.context_today(self)
        if not force and today.day != monthrange(today.year, today.month)[1]:
            return 0

        pending = self.sudo().search([
            ('state', '=', 'draft'),
            ('month', '=', str(today.month)),
            ('year', '=', today.year),
        ])
        if not pending:
            return 0

        # Re-derive the blocked flags before reporting on them: the write
        # paths keep them fresh, but a decision made outside those paths
        # (a direct SQL fix, a leave edited by a module that predates this)
        # would otherwise leave the digest describing yesterday's state.
        pending._recompute_blocked()

        by_manager = {}
        orphans = self.browse()
        for sheet in pending:
            manager_user = sheet.manager_id.user_id
            if manager_user:
                by_manager.setdefault(manager_user, self.browse())
                by_manager[manager_user] |= sheet
            else:
                orphans |= sheet

        sent = 0
        for manager_user, sheets in by_manager.items():
            self._notify_partners(
                manager_user.partner_id,
                _('Attendance sheets due today — %(count)s employee(s)',
                  count=len(sheets)),
                self._build_reminder_body(sheets, today),
            )
            sent += 1

        if orphans:
            # Nobody else to tell: these employees have no direct manager.
            hr_group = self.env.ref(
                'hr.group_hr_user', raise_if_not_found=False)
            if hr_group:
                self._notify_partners(
                    hr_group.user_ids.partner_id,
                    _('Attendance sheets with no direct manager — '
                      '%(count)s employee(s)', count=len(orphans)),
                    self._build_reminder_body(orphans, today, orphaned=True),
                )
                sent += 1

        _logger.info(
            'Attendance sheet reminder: notified %d recipient group(s) about '
            '%d unconfirmed sheet(s).', sent, len(pending),
        )
        return sent

    def _build_reminder_body(self, sheets, today, orphaned=False):
        """HTML body listing the employees whose month is still unconfirmed.

        Blocked employees are listed first and say what is blocking them —
        a flat list of names gives the supervisor no way to tell the ones
        they can fix in a click from the ones waiting on somebody else.
        """
        def _item(sheet):
            if sheet.x_is_blocked:
                return Markup(
                    '<li><b>%(name)s</b> — ⚠️ %(reason)s</li>'
                ) % {
                    'name': sheet.employee_id.name or '',
                    'reason': (sheet.x_blocked_reason or '').replace(
                        '\n', ' ').strip(),
                }
            return Markup(
                '<li>%(name)s — %(attended)s attended / %(absent)s absent</li>'
            ) % {
                'name': sheet.employee_id.name or '',
                'attended': sheet.total_attended,
                'absent': sheet.total_absent,
            }

        ordered = sheets.sorted(key=lambda s: (not s.x_is_blocked,
                                               s.employee_id.name or ''))
        items = Markup('').join(_item(sheet) for sheet in ordered)
        lead = (
            _('These employees have no Direct Manager, so nobody can confirm '
              'their attendance sheet:')
            if orphaned else
            _('Today is the last day of the month and these attendance '
              'sheets have not been sent to payroll yet:')
        )
        return Markup(
            '<strong>📋 %(title)s</strong><br/>'
            '%(lead)s<ul>%(items)s</ul>'
            '<i>Open <b>Attendance → Attendance Sheets</b>, check each month, '
            'then press <b>Confirm &amp; Send to Payroll</b>. Any sheet left '
            'unconfirmed is paid as <b>zero attendance</b>.</i>'
        ) % {
            'title': _('Attendance Sheets Awaiting Confirmation'),
            'lead': lead,
            'items': items,
        }




