from odoo import api, fields, models


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
    x_payment_term_from_bas = fields.Boolean(
        string='Payment Terms from BAS', copy=False,
        help='The Customer Payment Terms on this contact were set by the BAS '
             'customer sync (COD10.INVDAYS) and follow BAS. Changing the '
             'terms by hand clears this, and the sync then leaves them alone.',
    )

    def write(self, vals):
        if ('property_payment_term_id' in vals
                and not self.env.context.get('ksw_bas_payment_term')):
            vals = dict(vals, x_payment_term_from_bas=False)
        return super().write(vals)
