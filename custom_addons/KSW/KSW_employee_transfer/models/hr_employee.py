from odoo import _, fields, models


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    x_transfer_count = fields.Integer(compute='_compute_transfer_count', string='Transfers',
                                      groups='KSW_employee_transfer.group_transfer_hr')

    def _compute_transfer_count(self):
        counts = dict(self.env['ksw.employee.transfer'].sudo()._read_group(
            [('employee_id', 'in', self.ids)], ['employee_id'], ['__count']))
        for employee in self:
            employee.x_transfer_count = counts.get(employee, 0)

    def action_view_transfers(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'KSW_employee_transfer.action_ksw_employee_transfer')
        # Every transfer of his, closed ones included: this is his history.
        action.update(name=_('Transfers of %s', self.name), domain=[('employee_id', '=', self.id)],
                      context={'default_employee_id': self.id})
        return action
