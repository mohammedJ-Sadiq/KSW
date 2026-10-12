# -*- coding: utf-8 -*-
from odoo import api, models


class HrDepartment(models.Model):
    """Naming a GM on the department also qualifies him on the pay run.

    See KSW_base_security/models/hr_department.py for why the group list is
    collected through an override rather than declared centrally.
    """
    _inherit = 'hr.department'

    def _ksw_gm_capability_groups(self):
        return super()._ksw_gm_capability_groups() + [
            'KSW_commissions.group_commission_gm',
        ]

    @api.model
    def _ksw_grant_gm_capability(self, employees):
        # A department manager named GM already holds Supervisor; GM next
        # to it would not survive the next save of his user form.
        super()._ksw_grant_gm_capability(employees)
        employees.sudo().user_id._ksw_commission_sync_gm_role()

    def write(self, vals):
        res = super().write(vals)
        # Each GM has his own monthly run: a department that changes GM
        # takes its work in progress to the new GM's run.
        if {'x_gm_id', 'parent_id'} & set(vals):
            self.env['ksw.pay.submission']._rehome_all_open()
        return res
