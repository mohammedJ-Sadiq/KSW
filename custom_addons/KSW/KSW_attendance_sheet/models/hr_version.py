from odoo import api, models


class HrVersion(models.Model):
    _inherit = 'hr.version'

    @api.model_create_multi
    def create(self, vals_list):
        versions = super().create(vals_list)
        # sudo: contract_date_start is hr.group_hr_manager-only, and an HR
        # officer creating an employee creates its first version too.
        versions.sudo().filtered(
            'contract_date_start')._align_attendance_sheets()
        return versions

    def write(self, vals):
        res = super().write(vals)
        if 'contract_date_start' in vals:
            self._align_attendance_sheets()
        return res

    def _align_attendance_sheets(self):
        """A moved joining date moves where the employee's sheets start."""
        employees = self.sudo().employee_id
        if not employees:
            return
        self.env['ksw.attendance.sheet'].sudo().search([
            ('employee_id', 'in', employees.ids),
            ('state', '=', 'draft'),
        ])._align_to_employment()
