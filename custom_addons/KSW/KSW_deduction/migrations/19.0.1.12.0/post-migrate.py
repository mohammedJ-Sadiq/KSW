"""Stop payroll collections from predating their own charge.

`_settle_payslip_lines` used to date a payroll collection at the payslip's
period end. A deduction activated after that period closed (a loan
disbursed on 10 Sep whose August installment was collected afterwards)
therefore showed its collection on 31 Aug, before its charge on 10 Sep.
New collections are dated the day the payslip is confirmed.

The confirmation day of an old payslip was never recorded (`hr.payslip`
does not track its state), so it cannot be recovered. A collection cannot
happen before the installment exists, though, so the charge date is the
earliest true date: rows dated before it are moved up to it. Rows that were
already in order keep their period-end date. Manual settlements keep the
date the accountant entered. Commission settlements are re-dated by
KSW_commissions, which knows when each run was approved.

Raw SQL, as in the sibling migrations: `ksw.deduction` is a `mail.thread`
and an ORM write across historical rows would generate notification mail.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """
        UPDATE ksw_deduction_line AS l
           SET x_settlement_date = d.x_charge_date
          FROM ksw_deduction AS d
         WHERE d.id = l.deduction_id
           AND l.state = 'paid'
           AND l.payslip_id IS NOT NULL
           AND d.x_charge_date IS NOT NULL
           AND l.x_settlement_date < d.x_charge_date
        """
    )
    _logger.info(
        'Statement: %s payroll collections moved up to their charge date',
        cr.rowcount,
    )
