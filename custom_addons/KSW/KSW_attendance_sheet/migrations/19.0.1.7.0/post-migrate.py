"""Trim existing draft sheets to the employee's employment period.

September 2026. Sheets used to be generated for the whole calendar month
whatever the joining date, so a new joiner (contract start 18 Aug) got 31
attended days. Generation now starts on the joining date; this pass applies
the same rule to the draft sheets already in the database.

Confirmed sheets are deliberately left alone: their month has been released
to payroll (which already clamps its window to the contract start), and
changing a paid month is a Payslip Revision, not a data fix.
"""

import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    Sheet = env['ksw.attendance.sheet']
    drafts = Sheet.search([('state', '=', 'draft')])
    misaligned = drafts.filtered(lambda s: s._employment_window_blockers())
    misaligned._align_to_employment()
    _logger.info(
        'KSW_attendance_sheet: aligned %d of %d draft sheets to the '
        'employment period (ids %s).',
        len(misaligned), len(drafts), misaligned.ids,
    )
