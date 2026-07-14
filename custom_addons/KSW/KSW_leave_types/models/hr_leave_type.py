from odoo import fields, models


class HrLeaveType(models.Model):
    _inherit = 'hr.leave.type'

    is_sick_leave = fields.Boolean('Is Sick Leave')
    is_maternity_leave = fields.Boolean('Is Maternity Leave')
    is_paternity_leave = fields.Boolean('Is Paternity Leave')
    is_hajj_leave = fields.Boolean('Is Hajj Leave')
