"""Settle the sheets of vacations already under way (Sept 2026).

The DM dialog that used to do this is gone; vacations approved before this
release that are still running get their months settled now. DRAFT sheets
only — a month somebody already released to payroll is not reopened
retroactively.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    Leave = env['hr.leave']
    running = Leave.search([
        ('state', 'not in', ('refuse', 'cancel', 'draft')),
        ('x_return_state', '=', 'on_vacation'),
    ]).filtered(lambda l: Leave._settles_attendance_sheet(l))
    for leave in running:
        leave._settle_vacation_sheet(draft_only=True)
    _logger.info('KSW_unpaid_leave: settled draft sheets for %d running '
                 'vacation(s): %s', len(running), running.ids)
