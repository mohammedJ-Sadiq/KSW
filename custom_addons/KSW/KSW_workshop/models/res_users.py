from odoo import api, fields, models


class ResUsers(models.Model):
    _inherit = 'res.users'

    # Its own list, not KSW_base_security's x_assisted_manager_ids: that one
    # carries time off and loan scope, and an HR assistant is not thereby a
    # workshop one (Ahmed Moftah holds the HR role on KSWCO).
    x_workshop_assisted_manager_ids = fields.Many2many(
        'res.users',
        relation='ksw_workshop_assistant_rel',
        column1='assistant_id',
        column2='manager_id',
        string='Added Workshop Requesters',
        domain=[('share', '=', False)],
        help='Workshop requesters this user raises requests for although he '
             'does not report to them. Kept by the inverse of '
             'x_workshop_requester_for_ids; his own manager is included '
             'automatically unless excluded.')
    # The other half of the override: his own manager, taken out by an
    # administrator. Kept as an exclusion rather than folding the manager into
    # the added list, so a manager change on the employee still carries over.
    x_workshop_excluded_manager_ids = fields.Many2many(
        'res.users',
        relation='ksw_workshop_assistant_excluded_rel',
        column1='assistant_id',
        column2='manager_id',
        string='Excluded Workshop Requesters')
    # What the administrator edits: own manager by default, plus or minus his
    # changes. Has no effect without the Requester Assistant role.
    x_workshop_requester_for_ids = fields.Many2many(
        'res.users',
        string='Raises Workshop Requests For',
        compute='_compute_workshop_requester_for_ids',
        inverse='_inverse_workshop_requester_for_ids',
        domain=[('share', '=', False)],
        help='The workshop requesters this user raises requests for. Starts as '
             'his own manager; add or remove requesters for the workshop only, '
             'his reporting line elsewhere is unchanged.')
    # Odoo's own notification_type is one setting for every app. This one
    # narrows it to the workshop: read by
    # ksw.workshop.request._notify_get_recipients, so the user's mail from
    # time off, loans, payroll... keeps following notification_type.
    x_workshop_notify_in_odoo = fields.Boolean(
        string='Workshop Notifications in Odoo Only',
        help='Workshop request notifications arrive in the Odoo inbox only, '
             'not by email. Notifications from other apps are not affected '
             'and keep following the Notification setting above.')
    # Shows the list in Access Rights only when the role that reads it is
    # chosen. all_group_ids, not has_group: the form changes groups unsaved,
    # and compared by .ids, since an unsaved record holds NewIds that `in`
    # does not match against a real group.
    x_workshop_is_assistant = fields.Boolean(
        compute='_compute_workshop_is_assistant')
    x_workshop_assists_display = fields.Char(
        string='Raises Workshop Requests For',
        compute='_compute_workshop_assists_display')

    @property
    def SELF_READABLE_FIELDS(self):
        return super().SELF_READABLE_FIELDS + ['x_workshop_notify_in_odoo']

    @property
    def SELF_WRITEABLE_FIELDS(self):
        return super().SELF_WRITEABLE_FIELDS + ['x_workshop_notify_in_odoo']

    @api.depends('group_ids')
    def _compute_workshop_is_assistant(self):
        assistant = self.env.ref('KSW_workshop.group_workshop_requester_assistant').id
        requester = self.env.ref('KSW_workshop.group_workshop_requester').id
        for user in self:
            ids = user.all_group_ids.ids
            user.x_workshop_is_assistant = assistant in ids and requester not in ids

    def _ksw_workshop_own_managers(self):
        self.ensure_one()
        return self.sudo().employee_ids.parent_id.user_id - self

    @api.depends('x_workshop_assisted_manager_ids', 'x_workshop_excluded_manager_ids',
                 'employee_ids.parent_id.user_id')
    def _compute_workshop_requester_for_ids(self):
        for user in self:
            user.x_workshop_requester_for_ids = (
                user._ksw_workshop_own_managers() - user.sudo().x_workshop_excluded_manager_ids
            ) | user.sudo().x_workshop_assisted_manager_ids

    def _inverse_workshop_requester_for_ids(self):
        for user in self:
            own = user._ksw_workshop_own_managers()
            wanted = user.x_workshop_requester_for_ids - user
            user.sudo().write({
                'x_workshop_assisted_manager_ids': [(6, 0, (wanted - own).ids)],
                'x_workshop_excluded_manager_ids': [(6, 0, (own - wanted).ids)],
            })

    @api.depends('group_ids', 'x_workshop_assisted_manager_ids', 'x_workshop_excluded_manager_ids',
                 'employee_ids.parent_id.user_id')
    def _compute_workshop_assists_display(self):
        for user in self:
            managers = self.browse(user._ksw_workshop_assisted_manager_ids())
            user.x_workshop_assists_display = ', '.join(managers.mapped('name')) or False

    def _ksw_workshop_assisted_manager_ids(self):
        """The requesters this user raises workshop requests for, his own
        manager first unless an administrator excluded him.

        Empty without the Requester Assistant role, and for anyone who is a
        Requester himself: he raises his own, with his own scope. A manager
        who is not a Requester is dropped, so an assistant of a manager with
        no workshop role gets nothing rather than somebody else's trucks.
        """
        self.ensure_one()
        if not self.has_group('KSW_workshop.group_workshop_requester_assistant') \
                or self.has_group('KSW_workshop.group_workshop_requester'):
            return []
        user = self.sudo()
        candidates = (user.employee_ids.parent_id.user_id - user.x_workshop_excluded_manager_ids) \
            | user.x_workshop_assisted_manager_ids
        return [
            manager.id for manager in candidates
            if manager != user and manager.active
            and manager.has_group('KSW_workshop.group_workshop_requester')
        ]

    def _ksw_workshop_driver_root_ids(self):
        """The employees whose reporting lines this user may name as a
        workshop driver: his requesters' employees for an assistant, his own
        otherwise. Read by rule_hr_employee_workshop_driver as `child_of`
        roots, so the line itself is resolved live at search time; only these
        roots sit in the rule cache (see _get_invalidation_fields)."""
        self.ensure_one()
        managers = self.browse(self._ksw_workshop_assisted_manager_ids())
        return (managers or self).sudo().employee_id.ids

    def _get_invalidation_fields(self):
        return super()._get_invalidation_fields() | {
            'x_workshop_assisted_manager_ids', 'x_workshop_excluded_manager_ids'}
