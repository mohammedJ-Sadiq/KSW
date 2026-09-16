# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class HrDepartment(models.Model):
    """Per-department approvers: the General Manager and the Accountant.

    Every GM approval step in KSW used to be resolved by group membership
    alone -- `has_group('...group_annual_leave_gm')` never looked at the
    record, so one GM group meant one GM over the whole company. KSW is not
    organised that way: each department has its own GM, and that person is
    normally NOT the department manager (`manager_id`, the DM who approves
    the first step).

    So authority moves onto the record's own data. The group still answers
    "may you act as a GM at all"; this field answers "for whom". A GM who is
    not named on a department cannot approve its requests -- there is no
    company-wide override, by design.

    September 2026: the accounting step of the time-off chain got the same
    treatment, for the same reason. An accountant who handles one department
    has no business reading, or being notified about, the requests of the
    other twenty-six.

    It is a Many2many where the GM is a Many2one, and that difference is the
    org, not a technicality. A department has one General Manager -- a
    person, answerable. Its accounting step is worked by a *team*: any of
    them may clear it, and the whole point of naming somebody on a single
    department is that that department is the only one they are in. So the
    question the record has to answer is "who may act here", which is a set,
    not "who is responsible here", which is one name.

    `x_effective_gm_id` / `x_effective_accountant_ids` are stored on purpose,
    and that is what makes the whole design work. An `ir.rule` domain can
    traverse `department_id.x_effective_gm_id.user_id` only if the column
    exists, and the fallback chain is variable-length (this department -> its
    parent -> its grandparent -> the company default), so it could never be
    spelled out as a domain either way.
    """
    _inherit = 'hr.department'

    x_gm_id = fields.Many2one(
        'hr.employee',
        string='General Manager',
        tracking=True,
        help="Approves the GM step of time off, loan and commission "
             "requests for this department. Leave empty to inherit the "
             "parent department's GM, then the company default.",
    )

    x_effective_gm_id = fields.Many2one(
        'hr.employee',
        string='Effective GM',
        compute='_compute_effective_gm',
        store=True,
        recursive=True,
        help="Technical: the GM actually responsible for this department "
             "once the parent departments and the company default have been "
             "taken into account. Record rules and approval guards read "
             "this, never x_gm_id.",
    )

    x_accountant_ids = fields.Many2many(
        'hr.employee',
        relation='ksw_department_accountant_rel',
        column1='department_id', column2='employee_id',
        string='Accounting Approvers',
        tracking=True,
        help="May approve the accounting step of time off requests for this "
             "department. Any one of them can clear the step. Leave empty to "
             "inherit the parent department's accountants, then the company "
             "default accounting team.",
    )

    x_effective_accountant_ids = fields.Many2many(
        'hr.employee',
        relation='ksw_department_effective_accountant_rel',
        column1='department_id', column2='employee_id',
        string='Effective Accounting Approvers',
        compute='_compute_effective_accountants',
        store=True,
        recursive=True,
        help="Technical: the accounting approvers actually responsible for "
             "this department once the parent departments and the company "
             "default have been taken into account. Record rules and "
             "approval guards read this, never x_accountant_ids.",
    )

    @api.depends('x_gm_id', 'parent_id', 'parent_id.x_effective_gm_id',
                 'company_id', 'company_id.x_default_gm_id')
    def _compute_effective_gm(self):
        for dept in self:
            dept.x_effective_gm_id = (
                dept.x_gm_id
                or dept.parent_id.x_effective_gm_id
                or dept.company_id.x_default_gm_id
            )

    @api.depends('x_accountant_ids', 'parent_id',
                 'parent_id.x_effective_accountant_ids',
                 'company_id', 'company_id.x_default_accountant_ids')
    def _compute_effective_accountants(self):
        """Inherit, never merge.

        A department that names anybody owns its accounting step outright:
        its list REPLACES what it would have inherited, it does not add to
        it. That is the whole point of naming somebody on one department --
        if the inherited team stayed, the department would still be worked
        by everybody and nothing would have been narrowed. HR that wants
        both names the team members alongside, which is one extra click and
        is unambiguous on the form.
        """
        for dept in self:
            dept.x_effective_accountant_ids = (
                dept.x_accountant_ids
                or dept.parent_id.x_effective_accountant_ids
                or dept.company_id.x_default_accountant_ids
            )

    @api.constrains('x_gm_id')
    def _check_gm_has_user(self):
        """A GM with no user account is an approval step nobody can clear.

        Caught here rather than at approval time: the symptom otherwise is a
        request that silently stalls with no button and no notified
        recipient, days after the department was edited.
        """
        for dept in self:
            if dept.x_gm_id and not dept.x_gm_id.sudo().user_id:
                raise ValidationError(_(
                    "%(name)s has no user account, so they cannot be the "
                    "General Manager of %(dept)s. Link a user to the "
                    "employee record first.",
                    name=dept.x_gm_id.name, dept=dept.display_name,
                ))

    @api.constrains('x_accountant_ids')
    def _check_accountants_have_users(self):
        """Same reasoning as `_check_gm_has_user`, for the accounting step.

        Checked per name rather than "at least one of them can log in":
        a team of three where one has no account looks complete on the form
        and quietly routes nothing to that person.
        """
        for dept in self:
            orphans = dept.x_accountant_ids.sudo().filtered(
                lambda e: not e.user_id)
            if orphans:
                raise ValidationError(_(
                    "%(names)s has no user account, so they cannot be an "
                    "Accounting Approver of %(dept)s. Link a user to the "
                    "employee record first.",
                    names=', '.join(orphans.mapped('name')),
                    dept=dept.display_name,
                ))

    # ------------------------------------------------------------------
    # Capability groups
    # ------------------------------------------------------------------
    def _ksw_gm_capability_groups(self):
        """XML ids of the groups a department GM needs to act as one.

        Empty here: this module knows nothing about time off, loans or
        commissions. Each chain appends its own group by overriding this,
        which keeps the dependency direction right -- KSW_base_security must
        not reference groups defined downstream of it.
        """
        return []

    def _ksw_accountant_capability_groups(self):
        """XML ids of the groups a department accountant needs.

        Separate from `_ksw_gm_capability_groups` rather than one merged
        list: the two roles are held by different people, and merging them
        would grant every GM the accounting groups and vice versa.
        """
        return []

    @api.model
    def _ksw_grant_capability(self, employees, group_xmlids):
        """Add these employees' users to every group in `group_xmlids`.

        Naming somebody as an approver is the whole setup: HR should not
        have to remember a second, invisible step in Settings, because
        forgetting it produces a request that stalls with no error and no
        button.

        Deliberately additive. Scope now comes from the department record,
        so a user left in a capability group after being replaced has
        nothing to act on -- whereas auto-revoking would silently strip a
        group somebody was granted for another reason.
        """
        groups = self.env['res.groups']
        for xmlid in group_xmlids:
            group = self.env.ref(xmlid, raise_if_not_found=False)
            if group:
                groups |= group
        if not groups:
            return
        for employee in employees:
            user = employee.sudo().user_id
            if not user:
                continue
            missing = groups.filtered(
                lambda g: user not in g.sudo().all_user_ids)
            if missing:
                missing.sudo().write({'user_ids': [(4, user.id)]})

    @api.model
    def _ksw_grant_gm_capability(self, employees):
        """Give these employees' users the GM capability groups of every chain.

        Called from two places, and it has to be both -- the company's
        default GM answers for every department with no GM of its own AND
        for the 103 employees with no department at all, so leaving him
        without the groups makes the entire fallback dead.
        """
        self._ksw_grant_capability(
            employees, self._ksw_gm_capability_groups())

    @api.model
    def _ksw_grant_accountant_capability(self, employees):
        """The accounting mirror of `_ksw_grant_gm_capability`."""
        self._ksw_grant_capability(
            employees, self._ksw_accountant_capability_groups())

    def _ksw_sync_gm_capability(self):
        self._ksw_grant_gm_capability(self.mapped('x_gm_id'))

    def _ksw_sync_accountant_capability(self):
        self._ksw_grant_accountant_capability(self.mapped('x_accountant_ids'))

    @api.model_create_multi
    def create(self, vals_list):
        departments = super().create(vals_list)
        departments.filtered('x_gm_id')._ksw_sync_gm_capability()
        departments.filtered('x_accountant_ids')._ksw_sync_accountant_capability()
        return departments

    def write(self, vals):
        res = super().write(vals)
        if 'x_gm_id' in vals:
            self.filtered('x_gm_id')._ksw_sync_gm_capability()
        if 'x_accountant_ids' in vals:
            self.filtered('x_accountant_ids')._ksw_sync_accountant_capability()
        return res
