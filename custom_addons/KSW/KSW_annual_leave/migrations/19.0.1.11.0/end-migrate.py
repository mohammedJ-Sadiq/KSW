"""Open 'On Vacation' on requests already past GM final approval.

Until now the stamp landed at HR's document confirmation (Step 6).  It now
lands at GM final approval (Step 5), because that is when the employee
actually leaves — HR's filing of the signed form can trail it by days, and
during that gap nothing marked the employee as away: no ribbon, no punch
alert, and the attendance sheet could be confirmed for a month the employee
was not there.

Requests sitting in that gap on the day of the upgrade would otherwise keep
'not_applicable' forever, since the stamp they were waiting for has moved
behind them.  Re-asking the rule fixes them; it is the same rule the running
code applies, so it is idempotent and safe to run twice.

end-migrate, not post-: `_uses_multi_step_chain` is extended by
KSW_unpaid_leave and KSW_eos_leave, which load after this module (gotcha #45).
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    leaves = env['hr.leave'].search([
        ('x_annual_approval_state', 'in',
         ('pending_employee_signature', 'approved')),
        ('state', 'in', ('confirm', 'validate')),
        ('x_return_state', '=', 'not_applicable'),
    ])
    if not leaves:
        _logger.info('KSW_annual_leave: no in-flight vacations to re-stamp.')
        return
    before = set(leaves.filtered(lambda l: l.x_return_state == 'on_vacation').ids)
    leaves._sync_gm_final_state()
    changed = leaves.filtered(lambda l: l.x_return_state == 'on_vacation')
    _logger.info(
        'KSW_annual_leave: opened On Vacation on %s request(s) already past '
        'GM final approval: %s',
        len(changed) - len(before), changed.ids,
    )
