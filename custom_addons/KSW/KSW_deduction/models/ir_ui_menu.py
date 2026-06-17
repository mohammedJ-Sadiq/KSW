from odoo import models, _


class IrUiMenu(models.Model):
    _inherit = 'ir.ui.menu'

    def load_menus(self, debug):
        menus = super().load_menus(debug)
        root = self.env.ref('KSW_deduction.menu_ksw_deduction_root', raise_if_not_found=False)
        if root and root.id in menus:
            user = self.env.user
            has_management_access = (
                user.has_group('KSW_deduction.group_deduction_officer')
                or user.has_group('KSW_deduction.group_loan_hr')
                or user.has_group('KSW_deduction.group_loan_acc')
                or user.has_group('KSW_deduction.group_loan_gm')
            )
            if not has_management_access:
                menus[root.id]['name'] = _('Loans')
        return menus
