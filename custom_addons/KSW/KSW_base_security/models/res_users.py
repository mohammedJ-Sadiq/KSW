# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ResUsers(models.Model):
    """Manager -> assistant delegation.

    A direct manager may nominate one or more assistants who PREPARE
    requests (time off of any type, loan requests) for that manager's
    direct reports. The manager keeps every approval step personally:
    nothing here grants approval authority anywhere in KSW.

    This is a deliberate **two-key** design. The link below is inert on
    its own -- `_ksw_assisted_manager_ids()` returns an empty list unless
    the assistant ALSO holds `group_manager_assistant`, which only an
    administrator can grant. Naming an assistant is therefore safe; the
    capability group is the real gate.

    Why the field lives on `res.users` rather than `hr.employee`: an
    `ir.rule` domain can only reference `user`, `company_id` and
    `company_ids`, and `Environment.user` is sudo'd (odoo/api.py). So
    `user.x_assisted_manager_ids.ids` resolves in Python at rule-compile
    time to a flat id list, producing a plain
    `('<path>.user_id', 'in', [4, 17])` term. That beats a dotted path
    into the m2m on three counts: no subquery, no comodel record-rule
    interaction, and an empty list degrades to "matches nothing" -- the
    correct fallback for a user nobody has delegated to.
    """
    _inherit = 'res.users'

    x_assistant_ids = fields.Many2many(
        'res.users',
        relation='ksw_manager_assistant_rel',
        column1='manager_id',
        column2='assistant_id',
        string='Assistants',
        domain=[('share', '=', False)],
        help="Users allowed to PREPARE time off and loan requests for this "
             "manager's direct reports. They can never approve any step. "
             "Has no effect unless the assistant also holds the "
             "'Manager Assistant' access right.",
    )

    # Same relation table with the columns swapped: a true inverse that
    # costs no extra storage.
    #
    # Writable on purpose. The link can be set from EITHER side, because an
    # administrator granting somebody the Manager Assistant right is looking
    # at the *assistant's* user form -- being forced to go find each
    # manager's record instead is how you end up with the right granted and
    # the delegation empty, which is silently inert. Nothing is widened by
    # this: writing res.users needs base.group_erp_manager either way, so
    # anyone who can set this could already have set x_assistant_ids on the
    # manager's record.
    x_assisted_manager_ids = fields.Many2many(
        'res.users',
        relation='ksw_manager_assistant_rel',
        column1='assistant_id',
        column2='manager_id',
        string='Managers I Assist',
        domain=[('share', '=', False)],
        help="Managers whose direct reports this user may prepare time off "
             "and loan requests for. Has no effect unless this user also "
             "holds the 'Manager Assistant' access right.",
    )

    x_has_manager_assistant_right = fields.Boolean(
        string='Has Manager Assistant Right',
        compute='_compute_x_manager_delegation_summary',
        help="Technical: drives the delegation banners on the user form.",
    )

    x_effective_managers_display = fields.Char(
        string='Currently Assists',
        compute='_compute_x_manager_delegation_summary',
        help="The managers whose teams this user can actually prepare "
             "requests for right now, automatic and explicit combined.",
    )

    @api.depends('group_ids', 'x_assisted_manager_ids',
                 'employee_ids.parent_id.user_id',
                 'employee_ids.leave_manager_id')
    def _compute_x_manager_delegation_summary(self):
        for user in self:
            has_right = user.has_group(
                'KSW_base_security.group_manager_assistant')
            user.x_has_manager_assistant_right = has_right
            if not has_right:
                user.x_effective_managers_display = False
                continue
            managers = self.browse(user._ksw_assisted_manager_ids())
            user.x_effective_managers_display = ', '.join(
                managers.mapped('name')) or False

    def _ksw_assisted_manager_ids(self):
        """Ids of the managers this user may prepare requests for.

        Two sources, unioned:

          1. **Automatic** -- this user's OWN manager (`parent_id.user_id`
             and `leave_manager_id` on their employee record). An assistant
             almost always reports to the manager they assist, so granting
             the access right is enough for the common case; nobody has to
             remember a second setup step that gives no feedback when it is
             missed.
          2. **Explicit** -- `x_assisted_manager_ids`, for an assistant who
             also supports a manager they do NOT report to (a shared
             secretary, a cross-department PA).

        Empty unless the capability group is held. This helper is the single
        source of truth: every Python guard in KSW_annual_leave and
        KSW_deduction calls it, AND the ir.rule domains call it too
        (`user._ksw_assisted_manager_ids()` -- verified to pass safe_eval),
        so the two can never disagree about scope.
        """
        self.ensure_one()
        if not self.has_group('KSW_base_security.group_manager_assistant'):
            return []
        user_sudo = self.sudo()
        manager_ids = set(user_sudo.x_assisted_manager_ids.ids)
        for employee in user_sudo.employee_ids:
            if employee.parent_id.user_id:
                manager_ids.add(employee.parent_id.user_id.id)
            if employee.leave_manager_id:
                manager_ids.add(employee.leave_manager_id.id)
        # Never yourself: an assistant who is also a manager must not gain
        # their own team through this route -- that is the Supervisor tier's
        # job, and it would silently widen scope on every self-managed record.
        manager_ids.discard(self.id)
        return sorted(manager_ids)

    # Both sides listed: the link is writable from either, and a constrains
    # only fires for the field actually present in vals.
    @api.constrains('x_assistant_ids', 'x_assisted_manager_ids')
    def _check_no_self_delegation(self):
        for user in self:
            if user in user.x_assistant_ids or user in user.x_assisted_manager_ids:
                raise ValidationError(
                    _("A user cannot be their own assistant."))
