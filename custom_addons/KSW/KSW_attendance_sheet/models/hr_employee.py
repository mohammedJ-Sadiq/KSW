from calendar import monthrange

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    x_is_attendance_sheet = fields.Boolean(
        string='Uses Attendance Sheet',
        default=False,
        groups='hr.group_hr_user',
        help='If checked, this employee\'s attendance is managed via '
             'the monthly attendance sheet by their manager, instead of '
             'biometric device punch-in/punch-out.',
    )
    x_attendance_sheet_count = fields.Integer(
        string='Attendance Sheet Count',
        compute='_compute_x_attendance_sheet_count',
    )

    def _compute_x_attendance_sheet_count(self):
        counts = self.env['ksw.attendance.sheet']._read_group(
            [('employee_id', 'in', self.ids)], ['employee_id'], ['__count'])
        by_employee = {employee.id: count for employee, count in counts}
        for emp in self:
            emp.x_attendance_sheet_count = by_employee.get(emp.id, 0)

    def action_view_attendance_sheets(self):
        """Open this employee's full attendance sheet history (all
        months/years, including locked past ones) for review."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Attendance Sheets',
            'res_model': 'ksw.attendance.sheet',
            'view_mode': 'list,form',
            'domain': [('employee_id', '=', self.id)],
            'context': {'default_employee_id': self.id},
        }

    def _attendance_sheet_missing_prerequisites(self):
        """Labels of what must be set before this employee may use a sheet.

        A sheet needs a manager (they confirm it) and a main schedule (it
        decides workdays). KSW_payroll adds the salary structure.
        """
        self.ensure_one()
        emp = self.sudo()
        missing = []
        if not emp.main_calendar_id:
            missing.append(_('Main Work Schedule'))
        if not emp.parent_id:
            missing.append(_('Manager'))
        return missing

    # Fields whose change can make an active sheet employee incomplete.
    # KSW_payroll adds struct_id.
    _ATTENDANCE_SHEET_PREREQUISITE_FIELDS = (
        'x_is_attendance_sheet', 'main_calendar_id', 'parent_id')

    def _check_attendance_sheet_prerequisites(self):
        """Refuse a sheet employee who lacks a prerequisite.

        Called from create()/write(), NOT @api.constrains: Odoo 19 runs
        constraints under sudo(), so they cannot tell a person in the UI
        from a cron. Superuser paths (crons, migrations, imports) are exempt
        like every other KSW role guard.
        """
        if self.env.su:
            return
        problems = []
        for emp in self.sudo().filtered('x_is_attendance_sheet'):
            missing = emp._attendance_sheet_missing_prerequisites()
            if missing:
                problems.append('%s: %s' % (emp.name, ', '.join(missing)))
        if problems:
            raise ValidationError(_(
                'The attendance sheet cannot be activated until these are '
                'set:\n%(problems)s',
                problems='\n'.join(problems),
            ))

    @api.model_create_multi
    def create(self, vals_list):
        employees = super().create(vals_list)
        # A module that still has values to settle on the new employees
        # (KSW_payroll: the salary structure) defers this and calls it itself.
        if not self.env.context.get('ksw_defer_sheet_finalize'):
            employees._finalize_attendance_sheet_on_create()
        return employees

    def _finalize_attendance_sheet_on_create(self):
        self._check_attendance_sheet_prerequisites()
        # Only a person creating the employee gets the sheet opened here;
        # system paths (imports, migrations) never did, and the monthly cron
        # or a later toggle of the flag opens it for them as before.
        if not self.env.su:
            self.sudo().filtered('x_is_attendance_sheet')._open_current_sheet()

    def write(self, vals):
        """Auto-create current-month attendance sheet when the flag is turned ON."""
        # Detect employees that are being switched ON
        newly_enabled = newly_disabled = self.env['hr.employee']
        if 'x_is_attendance_sheet' in vals:
            if vals['x_is_attendance_sheet']:
                newly_enabled = self.filtered(
                    lambda e: not e.x_is_attendance_sheet)
            else:
                newly_disabled = self.filtered('x_is_attendance_sheet')
                newly_disabled._check_sheets_removable()

        res = super().write(vals)

        if set(vals) & set(self._ATTENDANCE_SHEET_PREREQUISITE_FIELDS):
            self._check_attendance_sheet_prerequisites()
        newly_enabled._open_current_sheet()
        newly_disabled._remove_current_sheets()
        return res

    def _check_sheets_removable(self):
        """Refuse turning the flag off over a month already sent to payroll.

        The current month's sheet is deleted when the flag goes off, but a
        confirmed one has been released to payroll: it has to be withdrawn
        first, through the button that carries the withdraw authority rules.
        Superuser paths (imports, migrations) keep it and are not refused.
        """
        if self.env.su:
            return
        confirmed = self.env['ksw.attendance.sheet'].sudo() \
            ._current_and_later_sheets(self).filtered(
                lambda s: s.state == 'confirmed')
        if confirmed:
            raise ValidationError(_(
                'These attendance sheets were already sent to payroll. '
                'Withdraw them from payroll first, then turn off '
                '"Uses Attendance Sheet":\n%(sheets)s',
                sheets='\n'.join(confirmed.mapped('display_name')),
            ))

    def _remove_current_sheets(self):
        """Delete the current (and any later) month's draft sheet.

        Once the flag is off payroll reads the biometric path, so the sheet
        and its auto-generated attendance would otherwise count as real
        attended days. Past months are history and are left alone.
        """
        if not self:
            return
        # sudo: a side effect of an edit already authorised on hr.employee,
        # like _open_current_sheet — sheet access is scoped to the manager.
        sheets = self.env['ksw.attendance.sheet'].sudo() \
            ._current_and_later_sheets(self).filtered(
                lambda s: s.state == 'draft')
        for emp in self:
            removed = sheets.filtered(lambda s: s.employee_id == emp)
            if not removed:
                continue
            emp.sudo().message_post(body=Markup(
                '<strong>Attendance sheet removed</strong><br/>'
                '"Uses Attendance Sheet" was turned off, so %(sheets)s '
                'was deleted with its auto-generated attendance.'
            ) % {'sheets': ', '.join(removed.mapped('display_name'))})
        sheets.unlink()

    def _open_current_sheet(self):
        """Open this month's sheet for employees who do not have one yet."""
        if not self:
            return
        # sudo: opening the employee's first sheet is a side effect of
        # an edit already authorised on hr.employee, not an act on the
        # sheet itself. Sheet access is scoped to the employee's own
        # manager, so without this an HR user enabling the flag for
        # somebody else's report would be refused by the create rule —
        # and the employee would silently have no sheet at all.
        Sheet = self.env['ksw.attendance.sheet'].sudo()
        today = fields.Date.context_today(self)
        month = str(today.month)
        year = today.year

        existing = Sheet.search([
            ('employee_id', 'in', self.ids),
            ('month', '=', month),
            ('year', '=', year),
        ])
        existing_emp_ids = set(existing.mapped('employee_id').ids)

        month_end = today.replace(
            day=monthrange(today.year, today.month)[1])
        for emp in self:
            joining = Sheet._employment_start(emp)
            if joining and joining > month_end:
                # Joins in a later month; the monthly cron opens it.
                continue
            if emp.id not in existing_emp_ids:
                Sheet.create({
                    'employee_id': emp.id,
                    'month': month,
                    'year': year,
                })
