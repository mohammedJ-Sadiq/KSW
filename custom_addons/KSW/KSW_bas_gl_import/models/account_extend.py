from odoo import fields, models


class AccountGroup(models.Model):
    _inherit = 'account.group'

    x_bas_code = fields.Char(
        string='BAS Code', index=True, copy=False,
        help="Account code in BAS (COD10.DCODE1) for hierarchy levels 1-4.")


class AccountAccount(models.Model):
    _inherit = 'account.account'

    x_bas_code = fields.Char(
        string='BAS Code', index=True, copy=False,
        help="Leaf account code in BAS (COD10.DCODE1, DLEVEL=5).")
    x_bas_name_ar = fields.Char(string='BAS Name (Arabic)')
    x_bas_name_en = fields.Char(string='BAS Name (English)')


class AccountMove(models.Model):
    _inherit = 'account.move'

    # Idempotency key: FTYPE/FTYPE2/CODE2/NUMBER1 of the BAS voucher.
    # Unique so a re-run updates nothing and duplicates nothing.
    x_bas_key = fields.Char(string='BAS Voucher Key', index=True, copy=False)
    x_bas_ftype = fields.Char(string='BAS FTYPE', copy=False)
    x_bas_branch = fields.Char(string='BAS Branch', index=True, copy=False)

    # Odoo 19 dropped ``_sql_constraints`` -- it is silently ignored and only
    # logs "Model attribute '_sql_constraints' is no longer supported".  The
    # unique index was therefore NEVER created on the first pass; idempotency
    # held only because action_import also de-duplicates in Python.
    _x_bas_key_uniq = models.Constraint(
        'UNIQUE (x_bas_key)',
        'A journal entry for this BAS voucher already exists.')


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # A BAS "customer"/"supplier" is a leaf GL account code, so it is the
    # natural key back to BAS.
    x_bas_code = fields.Char(string='BAS Account Code', index=True, copy=False)
    x_bas_name_ar = fields.Char(string='BAS Name (Arabic)')
    # Raw TAX_ID as BAS holds it -- kept even when it fails the ZATCA format,
    # so invalid registrations are visible rather than silently discarded.
    x_bas_vat = fields.Char(string='BAS TAX_ID (raw)')


class ProductProduct(models.Model):
    _inherit = 'product.product'

    x_bas_code = fields.Char(string='BAS Item Code', index=True, copy=False)


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    x_bas_key = fields.Char(string='BAS Delivery Note', index=True, copy=False)
    x_bas_branch = fields.Char(string='BAS Branch', index=True, copy=False)

    _x_bas_key_uniq = models.Constraint(
        'UNIQUE (x_bas_key)', 'This BAS delivery note is already imported.')
