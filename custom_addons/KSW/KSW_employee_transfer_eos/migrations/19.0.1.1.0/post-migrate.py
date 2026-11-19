"""End of service decided before the decision opened the EOS request itself
(ER/2026/0223 on dev): those lines waited for HR in Employee Transfers. Open
their End of Service request now, dated the day the GM decided."""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    lines = env['ksw.employee.release.line'].search([('status', '=', 'terminating')])
    for line in lines:
        date = (line.decided_date or line.write_date).date()
        line._ksw_create_eos_leave(date)
        _logger.info('EOS request opened for %s (%s), termination date %s.',
                     line.employee_id.name, line.request_id.name, date)
    lines.request_id._sync_state()
