"""Un-cover the absences of every validated unpaid leave.

An unpaid leave used to be linked to the employee's absence records by
``_auto_link_absence_attendance()`` exactly like a sick leave or a business
trip, and the link set ``x_is_covered`` — which means "excused *and paid*".
So a month spent on unpaid leave was paid in full: the absent days landed in
WORK100 instead of ATT_ABS and ATTDED deducted nothing.  KSWCO leave 4838
(FAISAL KUNDEYIL MOHAMEDKUTTY, 2026-07-23 → 2026-08-24) is the case that
surfaced it — 21 covered days and 3 granted Fridays on the August payslip.

The code fix is ``hr.leave._excuses_absence()``, which now takes unpaid leaves
out of the paid set.  ``x_is_covered`` / ``x_net_is_absent`` /
``x_deduction_amount`` are all *stored* computes, though, so the rows written
before the fix keep their old values until something recomputes them — the ORM
has no reason to, since neither the leave nor the attendance changed.

This pass therefore does what approving the leave would do today:

1. recompute coverage and the deduction on every attendance linked to a
   validated unpaid leave, and
2. re-run the weekend grant over those ranges — a Friday inside the unpaid
   block was granted because the covered days either side counted as attended;
   with them uncovered the grant is no longer earned and is revoked.

Payslips already computed for those periods are NOT touched: an open draft
slip has to be recomputed by payroll, and a done one is a paid document.  The
affected employees/periods are logged so they can be found.

NOTE: this stage cannot finish the job.  `x_deduction_amount` is declared by
KSW_payroll, which loads *after* this module, so it is not in the registry
here and `modified()` has nothing to notify — the rows come out flagged absent
but still worth 0.  `19.0.1.2.1/end-migrate.py` completes the repair at the
`end` stage, where every module is loaded.
"""

import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})

    leaves = env['hr.leave'].search([
        ('state', '=', 'validate'),
        ('holiday_status_id.is_unpaid_leave', '=', True),
        ('x_attendance_ids', '!=', False),
    ])
    if not leaves:
        _logger.info('Unpaid coverage repair: nothing linked, nothing to do.')
        return

    attendances = leaves.mapped('x_attendance_ids')
    covered_before = attendances.filtered('x_is_covered')
    _logger.info(
        'Unpaid coverage repair: %d leave(s), %d linked attendance row(s), '
        '%d of them flagged covered.',
        len(leaves), len(attendances), len(covered_before),
    )

    attendances._recompute_deductions()
    env.flush_all()
    if 'x_deduction_amount' not in env['hr.attendance']._fields:
        _logger.info(
            'Unpaid coverage repair: KSW_payroll is not loaded yet, so '
            'x_deduction_amount is untouched here — 19.0.1.2.1/end-migrate '
            'finishes it.')

    result = env['biometric.attendance.sync']._regenerate_weekends_for_leaves(
        leaves)
    _logger.info(
        'Unpaid coverage repair: weekend re-check granted %d, revoked %d.',
        result['created'], result['revoked'],
    )

    for leave in leaves:
        _logger.info(
            'Unpaid coverage repair: %s %s → %s — recompute the payslip(s) '
            'covering this period.',
            leave.employee_id.name, leave.request_date_from,
            leave.request_date_to,
        )
