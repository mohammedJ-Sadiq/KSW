from odoo import api, fields, models
from odoo.fields import Domain

def _ksw_readable_name_search_domain(model, operator, value):
    """Domain over only the `_rec_names_search` fields the caller may READ,
    or None to fall through to the standard path.

    `_rec_names_search` below lists identifiers that carry
    `groups='hr.group_hr_user'` (`barcode`, and `x_employee_no` where a
    module adds it). Odoo generates a search condition for **every** field in
    that list and enforces the field-level ACL *during the search*, so any
    non-HR user with model read on `hr.employee` -- i.e. everyone in the
    Employees privilege tier (Employee / Supervisor / Supervisor Cascading) --
    got a hard `AccessError: ... the field "barcode" ...` the moment they
    typed a character into any Employee field. The dropdown listed fine while
    empty and died on the first keystroke, which is why it reads as "search is
    broken" rather than as a permissions problem.

    The fix is to drop the unreadable fields from the search, NOT to resolve
    them under sudo. Sudo-resolving would hand every internal user an SSN /
    badge-number lookup over the whole company, which is a privacy boundary
    KSW_deduction deliberately restricts to the deduction roles (see
    `_deduction_identifier_domain` there, and
    `TestEmployeeIdentifierSearch.test_plain_user_cannot_find_employee_by_ssn`
    which pins it). Filtering keeps matching *exactly* as it was for every
    role and only removes the crash.

    `ssnid` / `identification_id` are deliberately NOT filtered here: they
    carry no field-level `groups` (they are delegated from `hr.version` via
    `_inherits`), so they raise nothing -- hr.version's record rules simply
    make them match nothing for a non-HR user, which is the pre-existing
    behaviour this must preserve.
    """
    env = model.env
    if env.su or not value or env.user.has_group('hr.group_hr_user'):
        return None

    user = env.user
    readable = []
    for fname in (model._rec_names_search or [model._rec_name]):
        field = model._fields.get(fname)
        if field is None:
            continue
        groups = getattr(field, 'groups', None)
        if groups and not any(
            user.has_group(g.strip()) for g in groups.split(',') if g.strip()
        ):
            continue
        readable.append(fname)

    if len(readable) == len(model._rec_names_search or [model._rec_name]):
        # Nothing was restricted -- let the standard implementation run.
        return None
    return Domain.OR([Domain(fname, operator, value) for fname in readable])


class HrEmployee(models.Model):
    _inherit = 'hr.employee'
    _rec_names_search = [
        'name',
        'ssnid',
        'identification_id',
        'barcode',
        'work_email',
        'mobile_phone',
    ]

    @api.model
    def _search_display_name(self, operator, value):
        domain = _ksw_readable_name_search_domain(self, operator, value)
        if domain is None:
            return super()._search_display_name(operator, value)
        return domain

    x_auto_send_payslip = fields.Boolean(
        string='Auto-Email Payslip',
        default=False,
        groups='hr.group_hr_user',
        help="If enabled, a PDF copy of each confirmed payslip is emailed "
             "automatically to this employee's Work Email.",
    )
    x_salary_bank_account_id = fields.Many2one(
        'res.partner.bank',
        string='Salary Paying Bank Account',
        groups='hr.group_hr_user',
        tracking=True,
        domain="[('partner_id', '=', company_partner_id)]",
        help='Company bank account used to pay this employee\'s salary. '
             'This is the source account the accounting team transfers from, '
             'not the employee\'s personal bank account.',
    )
    x_employee_no = fields.Char(
        string='Employee No.',
        index=True,
        groups='hr.group_hr_user',
        help='Internal employee number used by HR.',
    )
    x_payslip_export_order = fields.Integer(
        string='Payslip File Order',
        default=0,
        index=True,
        groups='hr.group_hr_user',
        help='Lower numbers are exported first in payroll TXT/Excel files.',
    )
    x_exclude_from_payroll = fields.Boolean(
        string='Exclude from Payroll Batches',
        default=False,
        # Must carry an HR group like every other custom hr.employee field:
        # without it, this stored field is the only custom employee field that
        # leaks into the non-HR "public profile" fetch path, raising an
        # AccessError ("not available for employee public profiles") the moment
        # a regular employee opens their own Time Off form (which reads their
        # employee record via hr.employee.public). group_hr_payroll_user (used
        # by the payslip batch wizard) implies hr.group_hr_user, so the wizard
        # keeps full access. Not referenced in any invisible= expression, so
        # gotcha #31 does not apply.
        groups='hr.group_hr_user',
        help='If enabled, this employee is hidden from payslip batch generation '
             'and the skip log for all non-administrator users.',
    )
    company_partner_id = fields.Many2one(
        related='company_id.partner_id',
        store=False,
    )



class HrEmployeePublic(models.Model):
    """Same identifier search, on the model non-HR users actually reach.

    `hr.employee.search_fetch` / `._search` delegate the whole query to
    `hr.employee.public` for any user WITHOUT model-level read access on
    `hr.employee` (the HACK in `addons/hr/models/hr_employee.py`). Overriding
    only `hr.employee` would therefore be dead code for exactly the users the
    override exists for. Ids match between the two models, so the shared
    helper works for either.
    """
    _inherit = 'hr.employee.public'

    @api.model
    def _search_display_name(self, operator, value):
        domain = _ksw_readable_name_search_domain(self, operator, value)
        if domain is None:
            return super()._search_display_name(operator, value)
        return domain
