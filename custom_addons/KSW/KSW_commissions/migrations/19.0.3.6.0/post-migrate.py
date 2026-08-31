"""Date the installments that were settled out of commission.

`KSW_deduction`'s Statement of Account reads `x_settlement_date` to place
each collected installment on the ledger. Its own migration backfills the
two routes it can see — manual settlement and payroll — but the third
route lives here: `ksw.pay.run.line._apply_loan_offset` settles parked
installments out of the commission payment, leaving `payslip_id` NULL,
`is_manual` False and `manual_date` NULL. Those rows are invisible to
every fallback that module could write.

The link is `x_paid_via_pay_run_line_id`, and the honest date is the end
of the run's period — the same convention payroll uses (a payslip dates
its collection at `date_to`, not at the day the batch happened to be
confirmed).

Raw SQL, as in the sibling migration: `ksw.deduction` is a `mail.thread`
and an ORM write across historical rows would generate notification mail.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # `period` is the first day of the run's month; the collection is
    # dated at the month end so it sorts after everything scheduled
    # within that month, exactly like a payslip's `date_to`.
    cr.execute(
        """
        UPDATE ksw_deduction_line AS l
           SET x_settlement_date = (
                   date_trunc('month', r.period)
                   + INTERVAL '1 month' - INTERVAL '1 day'
               )::date
          FROM ksw_pay_run_line AS rl
          JOIN ksw_pay_run AS r ON r.id = rl.run_id
         WHERE l.x_paid_via_pay_run_line_id = rl.id
           AND l.x_settlement_date IS NULL
           AND l.state = 'paid'
           AND r.period IS NOT NULL
        """
    )
    _logger.info(
        'Statement backfill: %s commission-settled installment dates',
        cr.rowcount,
    )
