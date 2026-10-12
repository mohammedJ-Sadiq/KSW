# -*- coding: utf-8 -*-
from odoo import api, models

SUPERVISOR = 'KSW_commissions.group_commission_supervisor'
SUPERVISOR_GM = 'KSW_commissions.group_commission_supervisor_gm'
ADMINISTRATOR = 'KSW_commissions.group_commission_officer'


class ResUsers(models.Model):
    """Keep a supervisor who is also a department's GM able to approve.

    Commission Role is a single-select, so Supervisor and General Manager
    cannot be held side by side: the user form rewrites the privilege to one
    group on every save. Taha (Equipment Rental) was named his department's
    GM, then lost the GM group, and his handovers reached him with no
    Approve button while nobody else was allowed to approve them.
    """
    _inherit = 'res.users'

    @api.model
    def _ksw_named_gm_users(self, users):
        """The subset of ``users`` named GM of some department, or the
        company default GM (who answers for employees with no department)."""
        users = users.sudo()
        named = self.env['hr.department'].sudo().with_context(
            active_test=False).search([
                ('x_effective_gm_id.user_id', 'in', users.ids),
            ]).mapped('x_effective_gm_id.user_id')
        named |= self.env['res.company'].sudo().search([
            ('x_default_gm_id.user_id', 'in', users.ids),
        ]).mapped('x_default_gm_id.user_id')
        return users & named

    def _ksw_commission_sync_gm_role(self):
        """Upgrade a named GM who holds Supervisor to Supervisor & GM.

        Additive only, like the rest of the capability grants: scope comes
        from the department, so a role left behind grants nothing. An
        Administrator is left alone — that tier is chosen deliberately and
        already outranks Supervisor in the dropdown.
        """
        combined = self.env.ref(SUPERVISOR_GM, raise_if_not_found=False)
        if not combined:
            return
        for user in self._ksw_named_gm_users(self):
            if (user.has_group(SUPERVISOR)
                    and not user.has_group(SUPERVISOR_GM)
                    and not user.has_group(ADMINISTRATOR)):
                combined.sudo().write({'user_ids': [(4, user.id)]})

    def write(self, vals):
        res = super().write(vals)
        if 'group_ids' in vals:
            # HR picking "Supervisor" for a sitting GM drops the GM group
            # the department granted him; restore it as the combined role.
            self._ksw_commission_sync_gm_role()
        return res
