from odoo import api, fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # One BAS account code per contact, in Odoo's own Reference (Oct 2026).
    # It used to be two fields -- this one, set by the customer sync, and
    # KSW_bas_gl_import's x_bas_code, set by the GL import -- which made two
    # contacts for one BAS customer. Both names now alias `ref`.
    ref = fields.Char(
        copy=False,
        help='The BAS account code of this customer or vendor (COD10.DCODE1, '
             'e.g. "120301080"): the key every BAS sync and import matches on.')
    x_client_account_number = fields.Char(
        related='ref', readonly=False, string='Client Account Number',
        help='The BAS customer account code. Kept under this name for the '
             'callers that use it; stored as the contact\'s Reference.',
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
