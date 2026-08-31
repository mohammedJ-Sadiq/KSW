from odoo import api, fields, models


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

    def write(self, vals):
        """Auto-create current-month attendance sheet when the flag is turned ON."""
        # Detect employees that are being switched ON
        newly_enabled = self.env['hr.employee']
        if 'x_is_attendance_sheet' in vals and vals['x_is_attendance_sheet']:
            newly_enabled = self.filtered(lambda e: not e.x_is_attendance_sheet)

        res = super().write(vals)

        if newly_enabled:
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
                ('employee_id', 'in', newly_enabled.ids),
                ('month', '=', month),
                ('year', '=', year),
            ])
            existing_emp_ids = set(existing.mapped('employee_id').ids)

            for emp in newly_enabled:
                if emp.id not in existing_emp_ids:
                    Sheet.create({
                        'employee_id': emp.id,
                        'month': month,
                        'year': year,
                    })

        return res

