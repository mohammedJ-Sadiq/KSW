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
        string='Also Raises Workshop Requests For',
        domain=[('share', '=', False)],
        help='Workshop requesters this user raises requests for although he '
             'does not report to them. His own manager is included '
             'automatically. Has no effect without the Requester Assistant role.')
    x_workshop_assists_display = fields.Char(
        string='Raises Workshop Requests For',
        compute='_compute_workshop_assists_display')

    @api.depends('group_ids', 'x_workshop_assisted_manager_ids', 'employee_ids.parent_id.user_id')
    def _compute_workshop_assists_display(self):
        for user in self:
            managers = self.browse(user._ksw_workshop_assisted_manager_ids())
            user.x_workshop_assists_display = ', '.join(managers.mapped('name')) or False

    def _ksw_workshop_assisted_manager_ids(self):
        """The requesters this user raises workshop requests for, his own
        manager first.

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
        candidates = user.employee_ids.parent_id.user_id | user.x_workshop_assisted_manager_ids
        return [
            manager.id for manager in candidates
            if manager != user and manager.active
            and manager.has_group('KSW_workshop.group_workshop_requester')
        ]
