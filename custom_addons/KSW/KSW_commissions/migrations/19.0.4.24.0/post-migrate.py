"""Date commission-settled installments on the day the run was approved.

`_apply_loan_offset` used to date a collection out of commission at the end
of the run's period, so a loan disbursed in September whose August
installment the August run settled showed the collection on 31 Aug, before
its own charge. New settlements are dated at the run's `approved_date`, the
day the installment is actually taken out of the commission; this re-dates
the existing ones the same way.

Runs carried over from the old commission sheets may have no
`approved_date`; those rows are only moved up to their charge date if they
predate it, as KSW_deduction does for payroll collections.

`approved_date` is stored in UTC; the date is taken in Riyadh time, as
`fields.Date.context_today` does for the users. Raw SQL, as in the sibling
migrations: `ksw.deduction` is a `mail.thread`.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """
        UPDATE ksw_deduction_line AS l
           SET x_settlement_date = (
                   (r.approved_date AT TIME ZONE 'UTC')
                   AT TIME ZONE 'Asia/Riyadh'
               )::date
          FROM ksw_pay_run_line AS rl
          JOIN ksw_pay_run AS r ON r.id = rl.run_id
         WHERE l.x_paid_via_pay_run_line_id = rl.id
           AND l.state = 'paid'
           AND r.approved_date IS NOT NULL
        """
    )
    _logger.info(
        'Statement: %s commission collections re-dated to the approval day',
        cr.rowcount,
    )
    cr.execute(
        """
        UPDATE ksw_deduction_line AS l
           SET x_settlement_date = d.x_charge_date
          FROM ksw_deduction AS d, ksw_pay_run_line AS rl,
               ksw_pay_run AS r
         WHERE d.id = l.deduction_id
           AND l.x_paid_via_pay_run_line_id = rl.id
           AND r.id = rl.run_id
           AND r.approved_date IS NULL
           AND l.state = 'paid'
           AND d.x_charge_date IS NOT NULL
           AND l.x_settlement_date < d.x_charge_date
        """
    )
    _logger.info(
        'Statement: %s commission collections without an approval date '
        'moved up to their charge date', cr.rowcount,
    )
