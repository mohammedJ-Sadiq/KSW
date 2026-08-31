"""Backfill the three ledger dates the Statement of Account reads.

`x_charge_date`, `x_writeoff_date` and `x_settlement_date` are stamped
going forward by the transitions that cause them, but every row already in
the database predates the fields. Without a backfill the statement would
open every historical account at zero and show nothing before today.

Raw SQL on purpose: this touches every deduction and every settled
installment, and an ORM write would fire the tracking/chatter machinery on
`ksw.deduction` (a `mail.thread`) — which on a database this size means
thousands of notification emails to the company address. None of these
columns is tracked, computed, or read by a constraint, so SQL is both safe
and the only sane option here.

Timezone matters. The source stamps are `Datetime` columns stored in UTC,
and a bare `::date` cast would misdate every approval made between 00:00
and 03:00 Riyadh local by one day. Convert through the company timezone
first.
"""
import logging

logging.getLogger(__name__)
_logger = logging.getLogger(__name__)

# All KSW operations are Riyadh-local; the company has no other timezone.
_TZ = 'Asia/Riyadh'


def migrate(cr, version):
    if not version:
        return

    _backfill_charge_date(cr)
    _backfill_writeoff_date(cr)
    _backfill_settlement_date(cr)


def _backfill_charge_date(cr):
    """When the deduction was charged to the employee.

    Best available evidence, in descending order of precision:
    the loan's disbursement confirmation, its GM approval, else the
    creation stamp (which is what a non-loan deduction has, since those
    activate in one click on the day they are entered).

    Draft records are left NULL — nothing has been charged yet.
    """
    cr.execute(
        """
        UPDATE ksw_deduction
           SET x_charge_date = (
                   COALESCE(disbursement_confirmed_date,
                            gm_approved_date,
                            create_date)
                   AT TIME ZONE 'UTC' AT TIME ZONE %s
               )::date
         WHERE x_charge_date IS NULL
           AND state IN ('active', 'completed', 'cancelled')
           AND COALESCE(disbursement_confirmed_date,
                        gm_approved_date,
                        create_date) IS NOT NULL
        """,
        (_TZ,),
    )
    _logger.info('Statement backfill: %s deduction charge dates', cr.rowcount)


def _backfill_writeoff_date(cr):
    """When a cancelled deduction stopped being collectable.

    There is no historical record of the cancellation moment — nothing
    ever stamped one — so `write_date` is the closest thing available. It
    is right whenever the cancellation was the last edit, which is the
    normal case for a cancelled record, and approximately right otherwise.
    Only the date the write-off lands on can be wrong; the amount cannot.
    """
    cr.execute(
        """
        UPDATE ksw_deduction
           SET x_writeoff_date = (
                   write_date AT TIME ZONE 'UTC' AT TIME ZONE %s
               )::date
         WHERE x_writeoff_date IS NULL
           AND state = 'cancelled'
        """,
        (_TZ,),
    )
    _logger.info('Statement backfill: %s deduction write-off dates',
                 cr.rowcount)


def _backfill_settlement_date(cr):
    """When each settled installment's money actually moved.

    Three sources, matching the three settlement routes:
      * manual settlement (including the payment wizard) → `manual_date`;
      * payroll collection → the payslip's period end, the same date
        `_settle_payslip_lines` now stamps;
      * commission offset (KSW_commissions) → handled by that module's own
        migration, which is the only place the pay run is visible.

    Anything left NULL falls back to `period_date` at read time, so a row
    missed here is dated by its scheduled month rather than dropped.
    """
    cr.execute(
        """
        UPDATE ksw_deduction_line
           SET x_settlement_date = manual_date
         WHERE x_settlement_date IS NULL
           AND state = 'paid'
           AND manual_date IS NOT NULL
        """
    )
    manual = cr.rowcount

    cr.execute(
        """
        UPDATE ksw_deduction_line AS l
           SET x_settlement_date = p.date_to
          FROM hr_payslip AS p
         WHERE l.payslip_id = p.id
           AND l.x_settlement_date IS NULL
           AND l.state = 'paid'
           AND p.date_to IS NOT NULL
        """
    )
    _logger.info(
        'Statement backfill: %s manual and %s payroll settlement dates',
        manual, cr.rowcount,
    )
