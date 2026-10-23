"""Settings > KSW Commissions > Sales & Collection from BAS.

Each field is one convention of the accountant's commission workbook,
stored as a system parameter and read by ``ksw.sales.commission.bas``.
Defaults reproduce the March 2026 workbook.
"""
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    ksw_sc_vat_rate = fields.Float(
        'VAT Rate to Remove (%)', default=15.0,
        config_parameter='ksw_commissions.sc_vat_rate',
        help='BAS amounts include VAT; commission is worked on amounts '
             'without it. 0 = use BAS amounts as they are.')
    ksw_sc_sales_doc_types = fields.Char(
        'Sales Document Types', default='002',
        config_parameter='ksw_commissions.sc_sales_doc_types',
        help='BAS voucher types (FTYPE) that count as a sale when they '
             'debit a customer, comma separated. 002 = month-end invoice.')
    ksw_sc_sales_return_doc_types = fields.Char(
        'Sales Return Document Types', default='',
        config_parameter='ksw_commissions.sc_sales_return_doc_types',
        help='Voucher types that reduce sales when they credit a customer. '
             'Empty = none.')
    ksw_sc_collection_doc_types = fields.Char(
        'Collection Document Types', default='018,015',
        config_parameter='ksw_commissions.sc_collection_doc_types',
        help='Voucher types that count as a collection when they credit a '
             'customer. 018 = cash receipt, 015 = bank receipt.')
    ksw_sc_ledger_prefixes = fields.Char(
        'Customer Ledgers in Scope', default='120301,120303,120306,120309',
        config_parameter='ksw_commissions.sc_ledger_prefixes',
        help='Customer account prefixes this commission covers, comma '
             'separated. * = every customer account.')
    ksw_sc_attribution = fields.Selection([
        ('document', 'Rep on each BAS document'),
        ('customer', "Customer's current rep"),
    ], string='Credit Sales & Collections To', default='document',
        config_parameter='ksw_commissions.sc_attribution',
        help='The rep BAS stamped on each invoice/receipt keeps past months '
             "right when customers are reassigned; the customer's current "
             'rep follows today\'s assignment.')
    ksw_sc_target_owner = fields.Selection([
        ('last_receipt', 'Rep on the last receipt'),
        ('customer', "Customer's current collector"),
    ], string='Collection Target Belongs To', default='last_receipt',
        config_parameter='ksw_commissions.sc_target_owner')
    ksw_sc_target_method = fields.Selection([
        ('due_balance', 'Balance due (as the workbook)'),
        ('fifo', 'FIFO aging (previous method)'),
    ], string='Collection Target Method', default='due_balance',
        config_parameter='ksw_commissions.sc_target_method',
        help='Balance due: balance at the end of the previous month minus '
             'the invoices still within the payment term.')
    ksw_sc_term_source = fields.Selection([
        ('invdays', 'Invoice payment term (INVDAYS)'),
        ('limt_days', 'Credit day limit (LIMT_DAYS)'),
    ], string='Payment Term From', default='invdays',
        config_parameter='ksw_commissions.sc_term_source')
    ksw_sc_default_term_days = fields.Integer(
        'Default Payment Term (Days)', default=30,
        config_parameter='ksw_commissions.sc_default_term_days',
        help='Used when BAS has no term for the customer.')
