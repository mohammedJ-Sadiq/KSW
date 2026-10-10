"""Lock every non-loan deduction type to a single installment (Oct 2026).

A non-loan deduction is now taken out of unpaid commission first
(KSW_commissions) and the rest by the next payslip, so it is one
installment. Loans keep their schedule. The system administrator can
unlock a type afterwards; this only sets the starting point.

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
           SET x_single_installment = TRUE
         WHERE COALESCE(is_loan, FALSE) = FALSE
        RETURNING id
        """
    )
    _logger.info('KSW_deduction: %s non-loan deduction types locked to a '
                 'single installment.', cr.rowcount)
