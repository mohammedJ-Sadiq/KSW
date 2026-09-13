"""Pull back every settlement date that was stamped in the future.

`_settle_payslip_lines` dated a collected installment at the payslip's
`date_to`, and `ksw.pay.run.line._apply_loan_offset` at the end of the run's
period. Both run on a *confirmation*, so a payslip or a commission run
confirmed **before** its period closes — a mid-month run, an off-cycle slip,
an arrears catch-up — stamped `x_settlement_date` in the future.

That is not a cosmetic wrong date. The Statement of Account drops every
movement dated after the day it is stated as of (`_split_movements`), and it
does so silently, while the charge row — dated at activation, in the past —
stays. So a collection made this morning vanished from this morning's
statement and the closing balance was overstated by exactly the amount just
collected. Found on KSWCO LO00085: three installments totalling 3,807.00
settled by payslip 18822, all three stamped 2026-09-30 and all three
invisible on 2026-09-13.

The repair reproduces what the fixed code would have stamped: the earlier of
the original stamp and the day the settling document was confirmed.

**That day comes from the payslip, not from the line.** The line's own
`write_date` looks like the obvious source and is wrong — anything that later
touches a deduction's schedule bumps it. On KSWCO the three LO00085 rows read
`write_date = 2026-09-13` because that morning's reschedule run had touched
them, while payslip 18822 was confirmed on **2026-09-10** (`create_date ==
write_date` to the second, state `done`: created and marked done in one
transaction). Taking the line's stamp would have dated the money three days
late — the same class of error this migration exists to correct. The line's
`write_date` is kept only as the fallback for a row with no payslip: a
commission-settled or manual line, where nothing better exists.

Only rows still dated in the future are touched, so the period-end convention is
left intact wherever it was already correct, and re-running costs nothing.

Raw SQL for the reasons the 19.0.1.5.0 backfill gives: `ksw.deduction.line`
hangs off a `mail.thread` parent, the column is neither tracked nor computed
nor read by a constraint, and an ORM write would mail the company address.
Riyadh-local before the `::date` cast, or a write made between 00:00 and
03:00 local is misdated by a day.
"""
import logging

_logger = logging.getLogger(__name__)

# All KSW operations are Riyadh-local; the company has no other timezone.
_TZ = 'Asia/Riyadh'


def migrate(cr, version):
    if not version:
        return

    cr.execute(
        """
        SELECT count(*), coalesce(sum(amount), 0)
          FROM ksw_deduction_line
         WHERE state = 'paid'
           AND x_settlement_date > CURRENT_DATE
        """
    )
    count, amount = cr.fetchone()
    if not count:
        _logger.info(
            'Settlement dates: none stamped in the future, nothing to repair.')
        return

    cr.execute(
        """
        UPDATE ksw_deduction_line AS l
           SET x_settlement_date = LEAST(
                   l.x_settlement_date,
                   (COALESCE(
                        (SELECT p.write_date FROM hr_payslip p
                          WHERE p.id = l.payslip_id),
                        l.write_date
                    ) AT TIME ZONE 'UTC' AT TIME ZONE %s)::date)
         WHERE l.state = 'paid'
           AND l.x_settlement_date > CURRENT_DATE
        """,
        (_TZ,),
    )
    repaired = cr.rowcount

    cr.execute(
        """
        SELECT count(*)
          FROM ksw_deduction_line
         WHERE state = 'paid'
           AND x_settlement_date > CURRENT_DATE
        """
    )
    left = cr.fetchone()[0]
    _logger.info(
        'Settlement dates: %s future-dated installment(s) worth %.2f found, '
        '%s repaired, %s still future-dated.',
        count, amount, repaired, left,
    )
