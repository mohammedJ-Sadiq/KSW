from odoo import api, fields, models


class HrLeaveType(models.Model):
    _inherit = 'hr.leave.type'
    # Types list by their configured Code, smallest first; types without a
    # numeric code go last, in their old sequence order.
    _order = 'x_code_sort asc nulls last, sequence, id'

    is_sick_leave = fields.Boolean('Is Sick Leave')
    is_maternity_leave = fields.Boolean('Is Maternity Leave')
    is_paternity_leave = fields.Boolean('Is Paternity Leave')
    is_hajj_leave = fields.Boolean('Is Hajj Leave')

    # `code` (om_hr_payroll) is a Char, so ordering on it directly would put
    # "10" before "9". This is the code zero-padded, stored so SQL can sort on
    # it. A Char and not an Integer: an empty Integer is stored as 0, which
    # would sort the uncoded types first instead of last.
    x_code_sort = fields.Char(
        'Code Sort Key', compute='_compute_x_code_sort', store=True,
        index=True)

    @api.depends('code')
    def _compute_x_code_sort(self):
        for leave_type in self:
            code = (leave_type.code or '').strip()
            leave_type.x_code_sort = code.zfill(12) if code.isdigit() else False

    @api.model
    def _model_sorting_key(self, leave_type):
        # Core's _search re-sorts the Time Off form's type picker in Python
        # (reverse=True) whenever an employee is in context, ignoring _order;
        # put the code in front of its key so the picker follows it too.
        code = leave_type.x_code_sort
        code_key = -int(code) if code else float('-inf')
        return (code_key, *super()._model_sorting_key(leave_type))
