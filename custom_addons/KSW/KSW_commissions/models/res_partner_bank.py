from odoo import fields, models


class ResPartnerBank(models.Model):
    """The BAS bank account behind a paying account.

    A commission settlement voucher credits the bank a transfer went out
    of. The paying account (Kawther WPS, Hayat Raj Cards…) is the company's
    account at the bank; this is the same account as BAS's ledger knows it
    (``1202…``). Typed, like the pay components' accounts: the BAS chart
    mirror carries no bank accounts.
    """
    _inherit = 'res.partner.bank'

    x_bas_account_code = fields.Char(
        string='BAS Account Code',
        help='The BAS ledger account (1202…) credited when commission is '
             'paid out of this account.')
    x_bas_account_name = fields.Char(string='BAS Account Name')
