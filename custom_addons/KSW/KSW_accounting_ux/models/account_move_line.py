from odoo import models


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    def _get_computed_taxes(self):
        """Fall back to the company's default sale/purchase tax.

        With no product, core takes the tax only from the account's default
        taxes.  The BAS chart was imported without any, so a bill typed against
        3101010001 posted with no VAT at all, while BAS books the same purchase
        with 15% input VAT.  The company default (Settings > Default Taxes) is
        what the chart template would have stamped on its own accounts.
        """
        taxes = super()._get_computed_taxes()
        if taxes or self.product_id or self.account_id.tax_ids:
            return taxes
        move = self.move_id
        if move.is_purchase_document(include_receipts=True):
            taxes = move.company_id.account_purchase_tax_id
        elif move.is_sale_document(include_receipts=True):
            taxes = move.company_id.account_sale_tax_id
        if taxes and move.fiscal_position_id:
            taxes = move.fiscal_position_id.map_tax(taxes)
        return taxes
