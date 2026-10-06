{
    'name': 'KSW Accounting UX',
    'version': '19.0.1.8.0',
    'author': 'Mohammed Albadr',
    'category': 'Accounting',
    'summary': 'Accounting menus laid out like Odoo Enterprise, plus the statements Community lacks',
    'description': """
Odoo Community ships the Enterprise *menu skeleton* under Accounting > Reporting
-- Statement Reports, Partner Reports, Taxes & Fiscal -- but leaves all three
EMPTY, because Enterprise fills them from `account_reports`, which Community does
not have. The OCA modules that replace those reports each hang their own
vendor-named menu next to them instead ("OCA accounting reports", "Taxes
Balance", "MIS Reporting").

The result is eight report menus, three of them empty, and the real reports
buried under a module name that means nothing to an accountant.

This module re-homes the reports into the standard groups and hides the empty
wrappers, so Reporting reads the way it does in Enterprise.

It then lays out the whole app the way Enterprise and SAP / Oracle divide the
work (Customers, Vendors, Bank and Cash, Accounting, Review, Reporting,
Configuration; see views/menus.xml), and adds what Community has no answer
for: Balance Sheet and Cash Flow Statement on the BAS chart, partner
statements reachable from a menu, Debit Notes, Payments Due, a cross-journal
Bank Reconciliation, Entries to Review, and menus for the existing core
Partner Ledger and Bills Analysis.
""",
    'depends': [
        'account', 'account_debit_note', 'account_financial_report',
        'account_tax_balance', 'mis_builder', 'mis_builder_budget',
        'account_asset_management', 'account_budget_oca', 'account_usability',
        'account_reconcile_oca', 'account_statement_base',
        'account_statement_import_file', 'account_statement_import_sheet_file_xlsx',
        'account_statement_import_file_reconcile_oca', 'account_move_template',
        'account_lock_date_update', 'account_fiscal_year',
        'account_fiscal_year_closing', 'account_chart_update',
        'date_range_account', 'partner_statement',
        'KSW_bas_gl_import',
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/bas_bank_journals.xml',
        'data/mis_income_statement.xml',
        'data/mis_balance_sheet.xml',
        'data/mis_cash_flow.xml',
        'wizard/pl_wizard_views.xml',
        'wizard/bs_cf_wizard_views.xml',
        'wizard/partner_statement_views.xml',
        'views/res_partner_views.xml',
        'views/actions.xml',
        'views/pl_depreciation_line_views.xml',
        'views/account_level_views.xml',
        'views/menus.xml',
    ],
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
