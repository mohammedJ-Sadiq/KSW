"""Finish the unpaid-coverage repair — the half `post-migrate` cannot reach.

`19.0.1.2.0/post-migrate.py` un-covers the absences of every validated unpaid
leave, but it can only recompute the fields that exist *at the moment it runs*.
A `post` script executes immediately after its own module is loaded, and
`hr.attendance.x_deduction_amount` is declared by **KSW_payroll**, which loads
several modules later:

    Loading module KSW_unpaid_leave (105/110)   ← post-migrate runs here
    ...
    Loading module KSW_payroll                  ← x_deduction_amount appears here

So `modified(['x_net_is_absent'])` had no `x_deduction_amount` in the registry
to notify, and the 38 repaired rows in KSWCO came out of the upgrade correctly
flagged absent while still carrying `x_deduction_amount = 0` — which is the
number `_worked_day_lines_biometric` actually sums into `ATT_ABS`. Half a
repair reads exactly like a whole one until you check the money.

An `end` script runs after **every** module is loaded (see
`odoo/modules/loading.py`, "STEP 3.5: execute migration end-scripts"), so the
full registry is available here. Anything in a migration that depends on a
field from a module further down the dependency chain belongs at this stage.

The pass is idempotent — it recomputes stored values from their inputs and the
weekend re-check skips what exists and revokes only what is no longer earned —
so it is safe on a database the post script already handled.
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
        _logger.info('Unpaid coverage repair (end): nothing linked.')
        return

    attendances = leaves.mapped('x_attendance_ids')
    has_deduction = 'x_deduction_amount' in env['hr.attendance']._fields
    before = (
        sum(attendances.mapped('x_deduction_amount')) if has_deduction else None
    )

    attendances._recompute_deductions()
    env.flush_all()

    result = env['biometric.attendance.sync']._regenerate_weekends_for_leaves(
        leaves)

    _logger.info(
        'Unpaid coverage repair (end): %d leave(s), %d row(s) — '
        'covered %d, absent %d, deduction %s → %s; weekends granted %d, '
        'revoked %d.',
        len(leaves), len(attendances),
        len(attendances.filtered('x_is_covered')),
        len(attendances.filtered('x_net_is_absent')),
        before,
        sum(attendances.mapped('x_deduction_amount')) if has_deduction else None,
        result['created'], result['revoked'],
    )

    for leave in leaves:
        _logger.info(
            'Unpaid coverage repair (end): %s %s → %s — recompute the '
            'payslip(s) covering this period.',
            leave.employee_id.name, leave.request_date_from,
            leave.request_date_to,
        )
