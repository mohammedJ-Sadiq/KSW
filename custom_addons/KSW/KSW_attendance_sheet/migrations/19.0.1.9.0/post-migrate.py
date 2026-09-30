"""Remove draft sheets left behind on employees no longer on a sheet.

September 2026. Turning "Uses Attendance Sheet" off used to leave the
current month's sheet, and its auto-generated attendance, in place — so the
biometric payroll path read a month of fabricated "present" days as real
punches (employee 4908). The flag-off now deletes it; this pass applies the
same rule to the sheets already orphaned.

The previous month is included so a deploy early next month still reaches a
sheet orphaned this month. Confirmed sheets are left alone: their month was
released to payroll.
"""

import logging

from odoo import SUPERUSER_ID, api, fields

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    today = fields.Date.context_today(env['ksw.attendance.sheet'])
    prev = (today.year, today.month - 1) if today.month > 1 \
        else (today.year - 1, 12)
    orphaned = env['ksw.attendance.sheet'].search([
        ('state', '=', 'draft'),
        ('employee_id.x_is_attendance_sheet', '=', False),
        ('year', '>=', prev[0]),
    ]).filtered(lambda s: (s.year, int(s.month)) >= prev)
    _logger.info(
        'KSW_attendance_sheet: removing %d orphaned draft sheets (ids %s, '
        'employees %s).', len(orphaned), orphaned.ids,
        orphaned.mapped('employee_id').ids)
    orphaned.unlink()
