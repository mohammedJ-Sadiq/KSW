"""Re-run of 19.0.1.5.0: its first KSWCO run aborted on a locked rest day
left attended (sheet 4559) after the version was already stamped.
Idempotent.


1. Sheets generated before a leave-aware generation existed: days an
   approved leave covers are still marked Attended (KSWCO sheet 4705, a
   vacation 19 May → 6 Sep whose September sheet opened on 1 Sep with 1–6
   Sep attended). Apply the coverage and lock, exactly as generation now
   does at birth.
2. The whole-month settlement no longer reaches past the return date in the
   return month. Release days it settled there, unless an approved leave
   (or an unconfirmed return) still covers them.

end-migrate: the coverage reads KSW_payroll's _leave_coverage_end and
KSW_leave_extension's lock hook, both loaded after this module.
Draft sheets only — a released month is a Payslip Revision, not a data fix.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    Line = env['ksw.attendance.sheet.line']

    settled = Line.search([
        ('x_settled_leave_id', '!=', False),
        ('x_leave_id', '=', False),
        ('sheet_id.state', '=', 'draft'),
    ])
    to_release = Line
    for line in settled:
        leave = line.x_settled_leave_id
        start = leave.request_date_from
        end = leave.request_date_to or start
        if (line.date <= end
                or (line.date.year, line.date.month)
                == (start.year, start.month)):
            continue
        if line.date in line.sheet_id._approved_leave_coverage([line.date]):
            continue
        to_release |= line
    if to_release:
        to_release.write({'x_settled_leave_id': False})
        to_release.with_context(ksw_system_write=True).write(
            {'is_attended': True})
    _logger.info('KSW_unpaid_leave: released %d return-month day(s) past the '
                 'return date: %s', len(to_release),
                 sorted(set(to_release.mapped('sheet_id').ids)))

    sheets = env['ksw.attendance.sheet'].search([('state', '=', 'draft')])

    def _clashes(sheet):
        covered = sheet._approved_leave_coverage(
            sheet.line_ids.mapped('date'))
        return any(l.is_attended and l.date in covered
                   for l in sheet.line_ids)

    clashing = sheets.filtered(_clashes)
    clashing.action_apply_approved_leave()
    _logger.info('KSW_unpaid_leave: applied approved time off to %d draft '
                 'sheet(s): %s', len(clashing), clashing.ids)
