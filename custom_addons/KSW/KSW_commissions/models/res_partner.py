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
    x_sales_rep_id = fields.Many2one(
        'hr.employee', string='Sales Rep',
        help='Employee who earns sales commission on this customer\'s '
             'BAS invoices. Used by "Pull from BAS" on the Sales/Collection '
             'Commission Sheet to attribute achieved sales to the right '
             'employee. Not restricted to attendance-sheet employees — '
             'most sales/collection reps are not that type.',
    )
    x_collection_rep_id = fields.Many2one(
        'hr.employee', string='Collection Rep',
        help='Employee who earns collection commission on this customer\'s '
             'BAS receipts. Used by "Pull from BAS" on the Sales/Collection '
             'Commission Sheet to attribute achieved collection to the '
             'right employee. Not restricted to attendance-sheet employees '
             '— most sales/collection reps are not that type.',
    )



