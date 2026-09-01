"""Open the return-confirmation gate on unpaid leaves that are still live.

Unpaid leave now uses the same return confirmation as annual leave: the direct
manager records the day the employee actually came back, and until they do the
payslip batch skips that employee. `_action_validate` stamps
`x_return_state = 'on_vacation'` from now on, but leaves validated before this
release are still sitting at `not_applicable`.

**Which of them to open is the whole question.** KSWCO has 118 validated unpaid
leaves with no stamp. Opening all of them would block payroll for 118 employees
over leave that mostly ended in 2025 — a gate nobody can close, on money long
since paid. Opening none of them would leave the two leaves behind the bug that
prompted this change (4838, 4927) sailing through the very batch the gate
exists to stop.

Two conditions, both of which a person would ask out loud:

1. **Is the money already out?** No `done` payslip covers the leave's end date.
   On its own this selects 58 leaves — because 55 of them predate payroll in
   Odoo entirely (33 ending May 2025, 14 in July 2025), so of course nothing
   was ever confirmed for them.
2. **Is it within payroll's horizon?** The leave ends on or after the first day
   of the *previous* month — the month being processed, plus the one before it.

Together they select exactly the live cases: in KSWCO, leaves 4838
(FAISAL, → 24 Aug), 4927 (AHMED SAMY, → 16 Aug) and 5078 (NAZEER, → 25 Sep).

Each stamped leave's direct manager is notified the same way a fresh approval
notifies them: a gate nobody was told about is just a stuck payroll.

`end` (not `post`): condition 1 reads `hr.payslip`, which belongs to
KSW_payroll — a module this one does not depend on and which loads *after* it.
See 19.0.1.2.1/end-migrate.py for the time that trap was paid for in full.
"""

import logging
from datetime import date, timedelta

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})
    Payslip = env['hr.payslip'].sudo()

    this_month = date.today().replace(day=1)
    horizon = (this_month - timedelta(days=1)).replace(day=1)

    candidates = env['hr.leave'].search([
        ('state', '=', 'validate'),
        ('holiday_status_id.is_unpaid_leave', '=', True),
        ('x_return_state', '=', 'not_applicable'),
    ])

    def already_paid(leave):
        return bool(Payslip.search_count([
            ('employee_id', '=', leave.employee_id.id),
            ('state', '=', 'done'),
            ('date_from', '<=', leave.request_date_to),
            ('date_to', '>=', leave.request_date_to),
        ]))

    live = candidates.filtered(
        lambda l: l.request_date_to
        and l.request_date_to >= horizon
        and not already_paid(l)
    )

    _logger.info(
        'Unpaid return gate: %d validated unpaid leave(s) carry no return '
        'stamp; %d of them end on or after %s with no confirmed payslip '
        'covering the period, and are being opened.',
        len(candidates), len(live), horizon,
    )
    if not live:
        return

    live.write({'x_return_state': 'on_vacation'})
    live._notify_return_confirmation_due()

    for leave in live:
        manager = leave.employee_id.leave_manager_id
        _logger.info(
            'Unpaid return gate: %s %s → %s — waiting on %s to confirm the '
            'return; the employee is skipped from the payslip batch until '
            'then.',
            leave.employee_id.name, leave.request_date_from,
            leave.request_date_to,
            manager.name if manager else 'NOBODY (no Time Off manager set)',
        )
