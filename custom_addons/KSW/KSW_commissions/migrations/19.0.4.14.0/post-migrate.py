"""Park the standing instructions that cannot produce a valid entry.

19.0.4.14.0 makes the reason part of a recurring entry's identity, and
required on a component that asks for one — a catch-all like Other exists
so an employee can carry several unrelated monthly payments, and the
reason is the only thing telling them apart.

That constraint arrives with a row already in the field that predates it:
a half-saved Other instruction with no reason and no amount. It was never
harmless. ``_apply_to_batch`` copies the reason onto the entry it creates,
and ``ksw.pay.entry._check_reason_required`` refuses an entry without one
— for the whole ``create(vals_list)``, so one incomplete row costs an
entire department its Add Recurring press. That failure was simply waiting
for somebody to press the button.

Archived rather than deleted, and rather than invented a reason for:
nobody here knows what the supervisor meant by it, an archived row is
skipped by the pull (``search`` filters on ``active``) and can be brought
back and completed in one click, and a deleted one cannot. The same
reasoning as the FK-anchor placeholders a few modules over.

Raw SQL: an ORM write would trip the very constraint this is clearing up
after, and there is nothing here that needs a compute to run.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute("""
        UPDATE ksw_pay_recurring r
           SET active = FALSE
          FROM ksw_pay_component c
         WHERE c.id = r.component_id
           AND c.needs_reason
           AND COALESCE(BTRIM(r.reason), '') = ''
           AND r.active
     RETURNING r.id, r.employee_id, c.code
    """)
    parked = cr.fetchall()
    if not parked:
        _logger.info(
            'KSW_commissions: no reasonless recurring entries to park.')
        return

    for rec_id, employee_id, code in parked:
        _logger.warning(
            'KSW_commissions: archived recurring entry %s (employee %s, '
            'component %s) — it has no reason, so the entry it would '
            'create is invalid. Give it a reason and un-archive it.',
            rec_id, employee_id, code)
    _logger.warning(
        'KSW_commissions: parked %d reasonless recurring entr%s.',
        len(parked), 'y' if len(parked) == 1 else 'ies')
