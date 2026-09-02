from odoo import fields, models


class HrPayslipInput(models.Model):
    """Carry the *uncollected* half of a KSW deduction input.

    On an ordinary monthly payslip a pending installment the salary cannot
    afford is simply capped down (`hr.payslip._ksw_apply_deduction_priority`)
    and the shortfall is invisible.  On a vacation / EOS payslip the whole
    obligation must stay on the document so the accountant sees the real
    picture, even when that drives the NET negative — so the input keeps its
    FULL amount and the part the pay could not absorb is recorded here.

    `amount - x_ksw_uncollected` is what `_sync_deductions_on_done` settles
    as paid; the remainder stays pending and rolls into a later payroll run.
    Default 0.0 means "everything shown was collected", which is exactly the
    right reading for every payslip computed before this field existed.
    """
    _inherit = 'hr.payslip.input'

    x_ksw_uncollected = fields.Float(
        string='Not Collected',
        digits=(16, 2),
        default=0.0,
        copy=False,
        help='Part of this installment the payslip could not afford. It is '
             'shown on the payslip for visibility but is NOT settled as '
             'paid — the installment stays pending for this amount and is '
             'collected by a later payroll run.',
    )
