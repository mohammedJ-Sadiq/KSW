"""Extends res.partner with commission import fields.

``x_client_account_number`` is defined in KSW_ext_sync (the BAS↔contact
link field, also used by ``ksw.bas.customer`` matching) — KSW_commissions
depends on KSW_ext_sync, so it's available here without redeclaring it.

``x_commission_import_name`` — name alias fallback for cases where the
account number is unavailable or not yet filled in.
"""
from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    x_commission_import_name = fields.Char(
        string='Commission Import Name',
        help='Customer name exactly as it appears in the accountant\'s '
             'monthly Sales Excel file (col 1 = Customer name). '
             'Fallback when Commission Account Number is blank. '
             'Leave blank to fall back to the partner\'s regular Name.',
    )
    # The sales rep is Odoo's own Salesperson (user_id, Sales & Purchase tab)
    # since Oct 2026: `x_sales_rep_id` (an employee) was a second field for the
    # same fact. Commissions read the Salesperson's employee
    # (`_ksw_sales_rep_employee`); migrations/19.0.4.54.0 moved the values.

    x_collection_rep_id = fields.Many2one(
        'hr.employee', string='Collection Rep',
        help='Employee who earns collection commission on this customer\'s '
             'BAS receipts. Used by "Pull from BAS" on the Sales/Collection '
             'Commission Sheet to attribute achieved collection to the '
             'right employee. Not restricted to attendance-sheet employees '
             '— most sales/collection reps are not that type.',
    )

    def _ksw_sales_rep_employee(self):
        """The employee behind this customer's Salesperson, as the manual
        sales-rep override for commissions (empty when none)."""
        self.ensure_one()
        return self.sudo().user_id.employee_id
