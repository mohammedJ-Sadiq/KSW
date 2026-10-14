from odoo import models


class IrUiMenu(models.Model):
    _inherit = 'ir.ui.menu'

    def _load_menus_blacklist(self):
        """A Reviewer who cannot raise requests has no "My Requests": it would
        always be empty, and he reads every request under All Requests. With
        it gone, the Workshop app opens on All Requests for him.

        Not a groups= on the menu: everyone else keeps it, including employees
        who cannot raise requests any more but own old ones (some own 170).
        """
        res = super()._load_menus_blacklist()
        user = self.env.user
        if user.has_group('KSW_workshop.group_workshop_reviewer') and not any(
                user.has_group(xmlid) for xmlid in (
                    'KSW_workshop.group_workshop_requester',
                    'KSW_workshop.group_workshop_requester_assistant',
                    'KSW_workshop.group_workshop_manager')):
            menu = self.env.ref('KSW_workshop.menu_ksw_workshop_my', raise_if_not_found=False)
            if menu:
                res.append(menu.id)
        return res
