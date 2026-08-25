# -*- coding: utf-8 -*-
from odoo import models


class HrDepartment(models.Model):
    """Naming a GM on the department also qualifies him on the loan chain.

    See KSW_base_security/models/hr_department.py for why the group list is
    collected through an override rather than declared centrally.
    """
    _inherit = 'hr.department'

    def _ksw_gm_capability_groups(self):
        return super()._ksw_gm_capability_groups() + [
            'KSW_deduction.group_loan_gm',
        ]
