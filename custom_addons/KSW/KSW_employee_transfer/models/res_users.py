from odoo import models


class ResUsers(models.Model):
    _inherit = 'res.users'

    def _ksw_transfer_department_ids(self):
        """Departments whose employees this user may ask to move (or ask for):
        those he manages, with their sub-departments. Extended by Vehicle
        Allocation with the departments named on the user there."""
        self.ensure_one()
        employee = self.sudo().employee_id
        if not employee:
            return []
        Department = self.env['hr.department'].sudo().with_context(active_test=False)
        managed = Department.search([('manager_id', '=', employee.id)])
        return Department.search([('id', 'child_of', managed.ids)]).ids if managed else []
