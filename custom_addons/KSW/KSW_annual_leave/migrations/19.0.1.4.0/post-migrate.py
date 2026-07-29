# -*- coding: utf-8 -*-
"""Post-migration: repair leaves whose approval chain fell out of sync with
their leave type.

``x_annual_approval_state`` was only ever stamped in ``create()``.  A user who
created a request with a stock type (e.g. Sick) and then edited the record to
an Annual/EOS/Unpaid type left the field NULL forever — the KSW statusbar and
every approval button stayed hidden and the record fell back to the stock
2-step chain (real case: leave 4891).  The reverse edit left a stale
``pending_*`` value behind, which hid the *stock* statusbar instead.

Both directions are repaired here, for records that can still move
(draft/confirm).  Validated, refused and cancelled leaves are never touched:
their outcome is final and rewinding them would be worse than the bug.

Raw SQL on purpose — an ORM write would re-run ``_check_validity()`` (which can
raise on historic allocation drift and abort the whole upgrade) and would fire
``_notify_pending_approvers``, mailing every direct manager about months-old
requests.  No stored field depends on ``x_annual_approval_state``, so nothing
needs recomputing afterwards.

The validation types are spelled out literally rather than read from
``hr.leave._multi_step_validation_types()``: this script runs right after
KSW_annual_leave loads, before KSW_unpaid_leave is in the registry, so the hook
would not yet know about 'unpaid_multi' and every broken unpaid leave would be
silently skipped.
"""
import logging

_logger = logging.getLogger(__name__)

CHAIN_TYPES = ('annual_multi', 'unpaid_multi')
REPAIRABLE_STATES = ('draft', 'confirm')

# Per-step approver stamps — cleared alongside the state so a repaired record
# never carries an approval that belongs to a previous life of the request.
_STAMP_COLUMNS = (
    'x_dm_approved_by', 'x_dm_approved_date',
    'x_hr_approved_by', 'x_hr_approved_date',
    'x_gm_initial_approved_by', 'x_gm_initial_approved_date',
    'x_acc_approved_by', 'x_acc_approved_date',
    'x_gm_final_approved_by', 'x_gm_final_approved_date',
    'x_employee_signed_by', 'x_employee_signed_date',
)


def migrate(cr, version):
    if not version:
        return

    clear_stamps = ', '.join('%s = NULL' % col for col in _STAMP_COLUMNS)

    # Direction A — the type uses a KSW chain but the chain never started.
    cr.execute("""
        UPDATE hr_leave l
           SET x_annual_approval_state = 'pending_dm',
               {clear_stamps}
          FROM hr_leave_type t
         WHERE l.holiday_status_id = t.id
           AND t.leave_validation_type IN %(chain_types)s
           AND l.x_annual_approval_state IS NULL
           AND l.state IN %(states)s
     RETURNING l.id
    """.format(clear_stamps=clear_stamps),
        {'chain_types': CHAIN_TYPES, 'states': REPAIRABLE_STATES})
    started = [row[0] for row in cr.fetchall()]

    # Direction B — a stale chain stamp on a type that uses the stock flow.
    # COALESCE guards a NULL leave_validation_type: a bare NOT IN would drop
    # those rows instead of matching them.
    cr.execute("""
        UPDATE hr_leave l
           SET x_annual_approval_state = NULL,
               {clear_stamps}
          FROM hr_leave_type t
         WHERE l.holiday_status_id = t.id
           AND COALESCE(t.leave_validation_type, '') NOT IN %(chain_types)s
           AND l.x_annual_approval_state IS NOT NULL
           AND l.state IN %(states)s
     RETURNING l.id
    """.format(clear_stamps=clear_stamps),
        {'chain_types': CHAIN_TYPES, 'states': REPAIRABLE_STATES})
    cleared = [row[0] for row in cr.fetchall()]

    if started:
        _logger.info(
            'KSW_annual_leave: started the multi-step chain on %s leave(s) '
            'whose type uses it but had no approval state: %s',
            len(started), started)
    if cleared:
        _logger.info(
            'KSW_annual_leave: cleared a stale multi-step approval state on '
            '%s leave(s) whose type uses the stock flow: %s',
            len(cleared), cleared)
    if not started and not cleared:
        _logger.info(
            'KSW_annual_leave: no out-of-sync approval chains found.')
