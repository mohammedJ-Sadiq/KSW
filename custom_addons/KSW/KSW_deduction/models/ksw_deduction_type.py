from odoo import _, api, fields, models
from odoo.exceptions import UserError


class KswDeductionType(models.Model):
    _name = 'ksw.deduction.type'
    _description = 'KSW Deduction Type'
    _order = 'sequence, name'

    name = fields.Char(required=True, translate=True)
    code = fields.Char(
        required=True,
        help='Short unique code used on payslip input lines (e.g. LOAN, GOVPEN).',
    )
    category = fields.Selection([
        ('borrowed', 'Borrowed (with employee consent)'),
        ('company_paid', 'Company Paid (on behalf of employee)'),
    ], required=True, default='borrowed',
        help='Borrowed: employee receives money (loan, advance). '
             'Company-paid: company settles a cost on behalf of the employee '
             '(gov/internal penalty).',
    )
    is_loan = fields.Boolean(
        string='Loan (5-step Approval)',
        default=False,
        help='When enabled, this type triggers the full DM -> HR -> Accounting '
             '-> GM approval workflow. Otherwise the deduction is activated '
             'instantly on creation.',
    )
    default_installments = fields.Integer(
        default=1,
        help='Suggested number of installments when creating a new deduction '
             'of this type. Can be overridden per record.',
    )
    managed_by = fields.Selection([
        ('acc_data_entry', 'Accounting Data Entry'),
        ('accounting', 'Accounting (Loans)'),
    ], string='Managed By', required=True, default='acc_data_entry',
        help='Who can create and manually close (mark as paid) deductions '
             'of this type outside payroll. '
             'Accounting Data Entry: gov. penalties, internal penalties, '
             'salary advances. '
             'Accounting (Loans): loans, which additionally require the '
             '"Loan Modification: Full" privilege.',
    )
    payroll_priority = fields.Integer(
        string='Payroll Priority',
        default=100,
        help='Order in which this deduction type is collected when the '
             'employee salary cannot cover all deductions in a month. '
             'Lower is collected first (e.g. penalties before loans). '
             'The unaffordable remainder is forwarded to the next month.',
    )
    # Oct 2026: a non-loan deduction is one installment — it is taken out of
    # unpaid commission first (KSW_commissions) and the rest by the next
    # payslip. Only the system administrator may change that per type.
    x_single_installment = fields.Boolean(
        string='Single Installment Only',
        default=False,
        help='Deductions of this type are always one installment: the '
             'number of installments cannot be changed on the deduction or '
             'by Reschedule Installments. Only a system administrator can '
             'change this setting.',
    )
    description = fields.Text()
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _unique_code = models.Constraint(
        'UNIQUE(code)',
        'The deduction type code must be unique.',
    )
    _positive_installments = models.Constraint(
        'CHECK(default_installments >= 1)',
        'Default installments must be at least 1.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        self._check_single_installment_create(vals_list)
        return super().create(vals_list)

    def write(self, vals):
        if ('x_single_installment' in vals and not self.env.su
                and not self.env.user.has_group('base.group_system')):
            raise UserError(_(
                'Only a system administrator can lock or unlock the number '
                'of installments of a deduction type.'))
        return super().write(vals)

    def _check_single_installment_create(self, vals_list):
        if self.env.su or self.env.user.has_group('base.group_system'):
            return
        if any(v.get('x_single_installment') for v in vals_list):
            raise UserError(_(
                'Only a system administrator can lock the number of '
                'installments of a deduction type.'))
