"""Non-loan deduction types are no longer locked to one installment (Nov 2026).

19.0.1.13.0 locked every non-loan type to a single installment. They go
back to how they were before: one installment by default
(``default_installments``), but the person entering the deduction may
spread it over several months. What is collected is unchanged: a payslip
and a commission month only ever take installments that are already due.

The lock itself stays, for a system administrator to set on a type again.
Existing deductions keep the installments they already have.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """
        UPDATE ksw_deduction_type
           SET x_single_installment = FALSE
         WHERE COALESCE(is_loan, FALSE) = FALSE
           AND x_single_installment
        RETURNING id
        """
    )
    _logger.info('KSW_deduction: %s non-loan deduction types unlocked from '
                 'a single installment.', cr.rowcount)
