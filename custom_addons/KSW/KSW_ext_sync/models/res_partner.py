from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    x_client_account_number = fields.Char(
        string='Client Account Number',
        help='BAS customer account code (COD10.DCODE1, e.g. "120301080") '
             'for this contact. Set automatically by "Match / Create '
             'Contacts" on BAS Sync > Customers, or manually. Used '
             'elsewhere (e.g. KSW_commissions) as the primary key to match '
             'BAS sales/collection activity to this customer.',
    )
