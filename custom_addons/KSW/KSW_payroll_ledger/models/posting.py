"""Where each salary figure is booked: configuration, not code.

Built the way SAP and Oracle separate the questions (Nov 2026):

* **what** the figure is — the salary rule's Posting Category (SAP's
  symbolic account): Housing, Transport, Other allowance… A rule without one
  is the wage;
* **who** the employee is for the books — his Posting Group (SAP's employee
  grouping for account determination): Main Office, Jubail Camp, Transport,
  Separate Salaries (M), Al-Hayat Factory… set on the department, inherited
  down the tree, overridden on the employee;
* the **Posting Map** answers the two together: group × category → account,
  a row without a category meaning "everything else". A branch that books
  the whole salary on one account is one row; the main office is one row per
  allowance;
* an employee whose figures go somewhere else for a reason of his own has
  **Posting Exceptions** (Oracle's assignment-level costing);
* whose money it is — the **Entity** of the bank account that paid him
  (Al-Kawther, Al-Hayat), with its own payable — and whose staff he is — the
  entity of his department. **Paid for Another Entity** says what happens
  when they differ: a posting group to book him in, and/or the current
  accounts each side keeps with the other.

A figure no row answers goes to the suspense account in Settings, named on
the batch, so the batch still posts (Oracle's suspense costing); without a
suspense account the employee is held out, as before.
"""
from markupsafe import Markup

from odoo import api, fields, models

DEDUCTION_TREATMENTS = [
    ('spread', 'Spread over the earnings'),
    ('basic', 'Against the basic wage only'),
    ('own_account', "Employee's own account"),
]


class KswPayrollEntity(models.Model):
    _name = 'ksw.payroll.entity'
    _description = 'Payroll Entity (whose money, whose staff)'
    _order = 'sequence, id'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda s: s.env.company)
    is_default = fields.Boolean(
        string='Default Entity',
        help='The entity of a department or bank account that names none.')
    payable_account_id = fields.Many2one(
        'account.account', string='Salaries Payable', required=True,
        help='Credited with the net of every salary this entity pays.')
    default_group_id = fields.Many2one(
        'ksw.payroll.posting.group', string='Default Posting Group', required=True,
        help="For this entity's staff whose department names no posting group.")
    note = fields.Text()


class KswPayrollIntercompany(models.Model):
    _name = 'ksw.payroll.intercompany'
    _description = 'Salary Paid for Another Entity'
    _rec_name = 'payer_id'

    payer_id = fields.Many2one('ksw.payroll.entity', string='Paid by', required=True, ondelete='cascade')
    staff_entity_id = fields.Many2one(
        'ksw.payroll.entity', string='Staff of', required=True, ondelete='cascade')
    posting_group_id = fields.Many2one(
        'ksw.payroll.posting.group', string='Book them in',
        help='Posting group for these employees when this entity pays them. Empty: their own.')
    payer_account_id = fields.Many2one(
        'account.account', string="Due from the Staff's Entity",
        help="Debited with the net, in the paying entity's books (Al-Hayat paying a "
             "Kawther employee: 2101030003). Empty, with the next one: no current accounts.")
    staff_account_id = fields.Many2one(
        'account.account', string='Due to the Payer',
        help="Credited with the net instead of salaries payable, in the staff entity's "
             "books (1208010010).")
    note = fields.Text()

    _payer_staff_unique = models.Constraint(
        'unique(payer_id, staff_entity_id)', 'One row per paying entity and staff entity.')


class KswPayrollPostingCategory(models.Model):
    _name = 'ksw.payroll.posting.category'
    _description = 'Payroll Posting Category'
    _order = 'sequence, id'

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    rule_ids = fields.One2many('hr.salary.rule', 'x_posting_category_id', string='Salary Rules')
    note = fields.Text()

    _code_unique = models.Constraint('unique(code)', 'The category code must be unique.')


class KswPayrollPostingGroup(models.Model):
    _name = 'ksw.payroll.posting.group'
    _description = 'Payroll Posting Group'
    _order = 'sequence, name'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    map_ids = fields.One2many('ksw.payroll.posting.map', 'group_id', string='Accounts', copy=True)
    department_ids = fields.One2many('hr.department', 'x_posting_group_id', string='Departments')
    employee_count = fields.Integer(compute='_compute_employee_count')
    note = fields.Text()

    def _compute_employee_count(self):
        Employee = self.env['hr.employee'].sudo()
        for group in self:
            group.employee_count = Employee.search_count([('x_posting_group_id', '=', group.id)])


class KswPayrollPostingMap(models.Model):
    _name = 'ksw.payroll.posting.map'
    _description = 'Payroll Posting Map'
    _order = 'group_id, category_id desc, id'

    group_id = fields.Many2one('ksw.payroll.posting.group', required=True, ondelete='cascade')
    category_id = fields.Many2one(
        'ksw.payroll.posting.category', string='Category',
        help='Empty: the wage and every category without a row of its own.')
    account_id = fields.Many2one('account.account', required=True)

    _group_category_unique = models.Constraint(
        'unique(group_id, category_id)', 'One account per posting group and category.')
    # unique() lets NULLs repeat: two catch-all rows would leave the wage
    # account to whichever row the lookup met first.
    _group_catch_all_unique = models.UniqueIndex(
        '(group_id) WHERE category_id IS NULL',
        'One account per posting group for the wage and everything else.')


class KswPayrollEmployeePosting(models.Model):
    _name = 'ksw.payroll.employee.posting'
    _description = 'Payroll Posting Exception'

    employee_id = fields.Many2one('hr.employee', required=True, ondelete='cascade')
    category_id = fields.Many2one(
        'ksw.payroll.posting.category', string='Category',
        help='Empty: his wage and every category without a row of its own.')
    account_id = fields.Many2one('account.account', required=True)
    note = fields.Char(string='Reason')
    # What the exception changes, said in full: without it the list is a
    # category and an account and nobody can tell what was overridden.
    group_id = fields.Many2one(
        'ksw.payroll.posting.group', string='His Posting Group', compute='_compute_effect',
        compute_sudo=True, help='The group his department (or his own setting) puts him in.')
    default_account_id = fields.Many2one(
        'account.account', string='Instead of', compute='_compute_effect', compute_sudo=True,
        help='Where this part of his salary would go without the exception.')
    summary = fields.Char(string='What it does', compute='_compute_effect', compute_sudo=True)

    @api.depends('employee_id', 'category_id', 'account_id')
    def _compute_effect(self):
        Posting = self.env['ksw.payroll.posting']
        for row in self:
            employee = row.employee_id
            company = employee.company_id or self.env.company
            staff = Posting._staff_entity(employee, company) if employee else Posting.browse()
            group = Posting._posting_group(employee, staff, staff) if employee and staff \
                else self.env['ksw.payroll.posting.group']
            rows = group.map_ids
            default = (rows.filtered(lambda r: r.category_id == row.category_id)[:1] if row.category_id
                       else rows.browse()) or rows.filtered(lambda r: not r.category_id)[:1]
            row.group_id = group
            row.default_account_id = default.account_id
            what = row.category_id.name or self.env._('Whole salary')
            rest = rows.filtered(lambda r: not r.category_id)[:1].account_id
            if row.category_id:
                row.summary = self.env._(
                    '%(what)s → %(to)s instead of %(default)s; the rest of his salary stays '
                    'on %(rest)s (%(group)s)', what=what, to=row.account_id.code or '?',
                    default=default.account_id.code or '?', rest=rest.code or '?',
                    group=group.name or '?')
            else:
                row.summary = self.env._(
                    'Whole salary → %(to)s instead of %(group)s, except categories with an '
                    'exception of their own', to=row.account_id.code or '?', group=group.name or '?')

    _employee_category_unique = models.Constraint(
        'unique(employee_id, category_id)', 'One exception per employee and category.')
    _employee_catch_all_unique = models.UniqueIndex(
        '(employee_id) WHERE category_id IS NULL',
        'One whole-salary exception per employee.')


class KswPayrollPosting(models.AbstractModel):
    """Answers "which account" from the tables above. Read as superuser:
    whoever confirms a batch need not read accounting configuration."""
    _name = 'ksw.payroll.posting'
    _description = 'Payroll Account Determination'

    @api.model
    def _default_entity(self, company):
        return self.env['ksw.payroll.entity'].sudo().search(
            [('company_id', '=', company.id), ('is_default', '=', True)], limit=1)

    @api.model
    def _staff_entity(self, employee, company):
        dept = employee.sudo().department_id
        while dept:
            if dept.x_ledger_entity_id:
                return dept.x_ledger_entity_id
            dept = dept.parent_id
        return self._default_entity(company)

    @api.model
    def _payer_entity(self, bank, company):
        return bank.sudo().x_ledger_entity_id or self._default_entity(company)

    @api.model
    def _intercompany(self, payer, staff):
        if not payer or not staff or payer == staff:
            return self.env['ksw.payroll.intercompany']
        return self.env['ksw.payroll.intercompany'].sudo().search(
            [('payer_id', '=', payer.id), ('staff_entity_id', '=', staff.id)], limit=1)

    @api.model
    def _posting_group(self, employee, staff, payer):
        intercompany = self._intercompany(payer, staff)
        if intercompany.posting_group_id:
            return intercompany.posting_group_id
        employee = employee.sudo()
        if employee.x_posting_group_id:
            return employee.x_posting_group_id
        dept = employee.department_id
        while dept:
            if dept.x_posting_group_id:
                return dept.x_posting_group_id
            dept = dept.parent_id
        return staff.default_group_id

    @api.model
    def _account(self, employee, group, category):
        """His exceptions first, then his group's map; in each, the row for the
        category, else the row without one. Empty when nothing answers."""
        for rows in (employee.sudo().x_posting_exception_ids, group.sudo().map_ids):
            row = (rows.filtered(lambda r: r.category_id == category)[:1] if category else rows.browse()) \
                or rows.filtered(lambda r: not r.category_id)[:1]
            if row:
                return row.account_id
        return self.env['account.account']


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    x_posting_preview = fields.Html(
        string='Where His Salary Goes', compute='_compute_posting_preview', compute_sudo=True,
        sanitize=False, groups='hr.group_hr_user',
        help='Every part of his salary, the account it is booked to and why: his posting '
             'group or one of his exceptions. Paid by his own entity.')

    @api.depends('x_posting_group_id', 'x_posting_exception_ids.category_id',
                 'x_posting_exception_ids.account_id', 'department_id')
    def _compute_posting_preview(self):
        Posting = self.env['ksw.payroll.posting']
        categories = self.env['ksw.payroll.posting.category'].search([])
        _ = self.env._
        for employee in self:
            company = employee.company_id or self.env.company
            staff = Posting._staff_entity(employee, company)
            group = Posting._posting_group(employee, staff, staff) if staff else False
            if not group:
                employee.x_posting_preview = False
                continue
            rows = []
            for category in [self.env['ksw.payroll.posting.category']] + list(categories):
                account = Posting._account(employee, group, category)
                exceptions = employee.x_posting_exception_ids
                # Same order as _account: his row for the category, else his
                # row without one, before anything of the group's.
                by_exception = bool(category and exceptions.filtered(lambda r: r.category_id == category)) \
                    or bool(exceptions.filtered(lambda r: not r.category_id))
                source = _('his exception') if by_exception else group.name
                rows.append(Markup(
                    '<tr%(style)s><td>%(what)s</td><td>%(code)s %(name)s</td><td>%(source)s</td></tr>') % {
                    'style': Markup(' style="font-weight:bold"') if by_exception else Markup(''),
                    'what': category.name if category else _('Wage (basic and anything without a category)'),
                    'code': account.code or '?', 'name': account.name or _('no account: held out or suspense'),
                    'source': source})
            employee.x_posting_preview = Markup(
                '<table class="table table-sm o_ksw_posting_preview"><thead><tr><th>%(a)s</th>'
                '<th>%(b)s</th><th>%(c)s</th></tr></thead><tbody>%(rows)s</tbody></table>') % {
                'a': _('Part of his salary'), 'b': _('Booked to'), 'c': _('Because of'),
                'rows': Markup('').join(rows)}
