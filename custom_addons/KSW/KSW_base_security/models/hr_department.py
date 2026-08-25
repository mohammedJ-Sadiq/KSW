# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class HrDepartment(models.Model):
    """Per-department General Manager.

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

    `x_effective_gm_id` is stored on purpose, and that is what makes the
    whole design work. An `ir.rule` domain can traverse
    `department_id.x_effective_gm_id.user_id` only if the column exists, and
    the fallback chain is variable-length (this department -> its parent ->
    its grandparent -> the company default), so it could never be spelled
    out as a domain either way.
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

    @api.depends('x_gm_id', 'parent_id', 'parent_id.x_effective_gm_id',
                 'company_id', 'company_id.x_default_gm_id')
    def _compute_effective_gm(self):
        for dept in self:
            dept.x_effective_gm_id = (
                dept.x_gm_id
                or dept.parent_id.x_effective_gm_id
                or dept.company_id.x_default_gm_id
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

    @api.model
    def _ksw_grant_gm_capability(self, employees):
        """Give these employees' users the capability groups of every chain.

        Naming somebody as a GM is the whole setup: HR should not have to
        remember a second, invisible step in Settings, because forgetting it
        produces a request that stalls with no error and no button.

        Called from two places, and it has to be both -- the company's
        default GM answers for every department with no GM of its own AND
        for the 103 employees with no department at all, so leaving him
        without the groups makes the entire fallback dead.

        Deliberately additive. Scope now comes from the department record,
        so a user left in a GM group after being replaced has nothing to act
        on -- whereas auto-revoking would silently strip a group somebody was
        granted for another reason.
        """
        groups = self.env['res.groups']
        for xmlid in self._ksw_gm_capability_groups():
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

    def _ksw_sync_gm_capability(self):
        self._ksw_grant_gm_capability(self.mapped('x_gm_id'))

    @api.model_create_multi
    def create(self, vals_list):
        departments = super().create(vals_list)
        departments.filtered('x_gm_id')._ksw_sync_gm_capability()
        return departments

    def write(self, vals):
        res = super().write(vals)
        if 'x_gm_id' in vals:
            self.filtered('x_gm_id')._ksw_sync_gm_capability()
        return res
