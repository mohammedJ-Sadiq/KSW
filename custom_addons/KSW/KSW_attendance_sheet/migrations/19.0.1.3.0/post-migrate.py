"""Backfill the "Needs Attention" flags on existing draft sheets.

August 2026. `x_is_blocked` / `x_blocked_reason` / `x_action_owner_id` are
maintained by write-path triggers, which by definition only fire on records
that change afterwards. Without this pass every sheet already in the database
reads as unblocked until someone happens to touch it — and the whole point of
the flags is that a supervisor can trust the list to show them what needs
doing, on the first screen, before they touch anything.

Only draft sheets are evaluated: a confirmed one is settled, and
`_recompute_blocked` clears its flags anyway.
"""

import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # Normalise every row first, confirmed ones included. `_order` sorts
    # x_is_blocked DESC and Postgres puts NULLs FIRST on a DESC sort, so a
    # historical sheet with an unset flag would outrank a genuinely blocked
    # one and sit at the top of the very list it does not belong in.
    cr.execute("""
        UPDATE ksw_attendance_sheet
           SET x_is_blocked = false
         WHERE x_is_blocked IS NULL
    """)
    _logger.info(
        'Attendance sheet: normalised %d row(s) with an unset blocked flag.',
        cr.rowcount,
    )

    env = api.Environment(cr, SUPERUSER_ID, {})
    sheets = env['ksw.attendance.sheet'].search([('state', '=', 'draft')])
    if not sheets:
        return

    _logger.info(
        'Attendance sheet: backfilling blocked flags on %d draft sheet(s).',
        len(sheets),
    )

    blocked = 0
    for sheet in sheets:
        # One bad sheet (a deleted employee, a broken calendar) must not
        # abort the upgrade for the other 362.
        try:
            with cr.savepoint():
                sheet._recompute_blocked()
            if sheet.x_is_blocked:
                blocked += 1
        except Exception:
            _logger.exception(
                'Attendance sheet: could not evaluate blockers for sheet '
                'id=%s — leaving its flags unset.', sheet.id,
            )

    _logger.info(
        'Attendance sheet: %d of %d draft sheet(s) need attention.',
        blocked, len(sheets),
    )
