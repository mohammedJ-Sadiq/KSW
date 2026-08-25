# -*- coding: utf-8 -*-
from odoo import models


class HrDepartment(models.Model):
    """Naming a GM on the department is the whole setup for time off.

    KSW_base_security owns the field and the resolver but must not know the
    leave groups exist. Each chain declares its own capability group here,
    so HR sets one field and the person can act -- rather than setting a
    field, then hunting for an access right in Settings that gives no
    feedback when it is missed.
    """
    _inherit = 'hr.department'

    def _ksw_gm_capability_groups(self):
        return super()._ksw_gm_capability_groups() + [
            'KSW_annual_leave.group_annual_leave_gm',
        ]
