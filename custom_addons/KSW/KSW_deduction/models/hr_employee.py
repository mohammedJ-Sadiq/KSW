from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.fields import Domain

# Roles that work on deductions for the whole company but are NOT HR users:
# the accounting data-entry team and the loan approval/modification chain.
DEDUCTION_ROLE_GROUPS = (
    'KSW_deduction.group_acc_data_entry',
    'KSW_deduction.group_deduction_officer',
    'KSW_deduction.group_installment_edit',
    'KSW_deduction.group_loan_hr',
    'KSW_deduction.group_loan_acc',
    'KSW_deduction.group_loan_gm',
    'KSW_deduction.group_loan_disbursement',
)

# What those roles may type into an employee field to find someone.
# `ssnid` / `identification_id` live on hr.version (delegated through
# `_inherits`) and `x_employee_no` / `x_loan_acc_no` are gated to
# hr.group_hr_user, which is why the plain `_rec_names_search` path finds
# nothing for them — see `_search_display_name` below.
DEDUCTION_EMPLOYEE_SEARCH_FIELDS = (
    'name',
    'ssnid',
    'identification_id',
    'x_employee_no',
    'x_loan_acc_no',
    'barcode',
)

# Positive text operators we can safely re-express as an id whitelist.
# Negative ones ('not ilike', '!=') would invert into "every employee
# except the matches", which an id list cannot express here — those fall
# through to the standard behaviour.
_POSITIVE_TEXT_OPERATORS = ('like', 'ilike', '=like', '=ilike', '=')


def _deduction_identifier_domain(model, operator, value):
    """Domain matching employees by identifier, or None to fall through.

    Returns ``None`` unless the caller is one of the deduction roles that
    is NOT an HR user; those are the only ones the standard path fails for.
    """
    env = model.env
    if (
        env.su
        or not value
        or not isinstance(value, str)
        or operator not in _POSITIVE_TEXT_OPERATORS
        or env.user.has_group('hr.group_hr_user')
        or not any(env.user.has_group(g) for g in DEDUCTION_ROLE_GROUPS)
    ):
        return None

    Employee = env['hr.employee'].sudo()
    domain = Domain.OR([
        Domain(fname, operator, value)
        for fname in DEDUCTION_EMPLOYEE_SEARCH_FIELDS
        if fname in Employee._fields
    ])
    # hr.employee.public shares its ids with hr.employee (same SQL view),
    # so the id list is valid on either model.
    return Domain('id', 'in', Employee._search(domain))


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    @api.model
    def _search_display_name(self, operator, value):
        """Let deduction roles find employees by SSN / employee no. / ID.

        `KSW_payroll` already lists those identifiers in
        `_rec_names_search`, but they are restricted to hr.group_hr_user
        (`ssnid` and `identification_id` additionally sit on hr.version,
        whose record rules only expose the user's own versions). For an
        accounting data-entry user the generated conditions therefore match
        nothing at all — typing an SSN in the Employee field silently
        returns an empty list.

        Resolve the identifiers with `sudo()` and hand back the matching
        ids. This widens *matching* only: the caller's own record rules
        still filter the result, and no restricted value is ever returned
        to the user.
        """
        domain = _deduction_identifier_domain(self, operator, value)
        if domain is None:
            return super()._search_display_name(operator, value)
        return domain

    x_deduction_count = fields.Integer(
        string='Active Deductions',
        compute='_compute_deduction_count',
        groups='hr.group_hr_user',
    )
    x_deduction_monthly_total = fields.Monetary(
        string='Monthly Deduction Total',
        compute='_compute_deduction_count',
        groups='hr.group_hr_user',
        currency_field='x_deduction_currency_id',
        help='Pending installments this month\'s payroll will collect: '
             'those scheduled for the current month plus any still pending '
             'from earlier months.',
    )
    x_deduction_outstanding_total = fields.Monetary(
        string='Outstanding Deduction Total',
        compute='_compute_deduction_count',
        groups='hr.group_hr_user',
        currency_field='x_deduction_currency_id',
        help='All still-pending installments across every active deduction, '
             'regardless of the month they fall in.',
    )
    x_deduction_currency_id = fields.Many2one(
        'res.currency',
        compute='_compute_deduction_count',
        groups='hr.group_hr_user',
    )
    x_loan_acc_no = fields.Char(
        string='Loan Acc No. in Bas',
        groups='hr.group_hr_user',
        help='Employee loan account number in BAS (bank loan/financing system).',
    )

    def _compute_deduction_count(self):
        company_currency = self.env.company.currency_id
        today = fields.Date.context_today(self)
        period_start = today.replace(day=1)
        period_end = period_start + relativedelta(months=1, days=-1)
        for emp in self:
            active = self.env['ksw.deduction'].sudo().search([
                ('employee_id', '=', emp.id),
                ('state', '=', 'active'),
            ])
            emp.x_deduction_count = len(active)
            # `monthly` sums the pending installments this month's payroll will
            # actually collect: the ones scheduled for the current month PLUS
            # any left pending from an earlier month. A payslip that could not
            # afford an installment leaves the remainder pending with its
            # ORIGINAL period (see `_settle_payslip_lines`), and
            # `hr.payslip._ksw_pending_lines_domain` picks up everything with
            # `period_date <= date_to` — so the same `<=` rule is used here.
            # `outstanding` sums every pending installment regardless of month
            # (what the employee still owes in total).
            monthly = 0.0
            outstanding = 0.0
            for ded in active:
                for line in ded.line_ids:
                    if line.state != 'pending':
                        continue
                    outstanding += line.amount
                    if line.period_date and line.period_date <= period_end:
                        monthly += line.amount
            emp.x_deduction_monthly_total = monthly
            emp.x_deduction_outstanding_total = outstanding
            emp.x_deduction_currency_id = company_currency

    def action_view_deductions(self):
        self.ensure_one()
        return {
            'name': 'Deductions of %s' % self.name,
            'type': 'ir.actions.act_window',
            'res_model': 'ksw.deduction',
            'view_mode': 'list,form',
            'domain': [('employee_id', '=', self.id)],
            'context': {'default_employee_id': self.id},
        }


class HrEmployeePublic(models.Model):
    """Same identifier search, on the model non-HR users actually hit.

    `hr.employee.search_fetch` / `._search` delegate to `hr.employee.public`
    for any user without model-level read access on `hr.employee` (see the
    HACK in `addons/hr/models/hr_employee.py`). Without this second
    override the accounting data-entry team never reaches the one on
    `hr.employee`, and an SSN typed into an Employee field returns nothing.
    """
    _inherit = 'hr.employee.public'

    @api.model
    def _search_display_name(self, operator, value):
        domain = _deduction_identifier_domain(self, operator, value)
        if domain is None:
            return super()._search_display_name(operator, value)
        return domain
