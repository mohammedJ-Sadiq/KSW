{
    'name': 'KSW Accounting UX',
    'version': '19.0.1.0.0',
    'author': 'Mohammed Albadr',
    'category': 'Accounting',
    'summary': 'Organise the Accounting menus into the shape Odoo Enterprise uses',
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
""",
    'depends': [
        'account', 'account_financial_report', 'account_tax_balance',
        'mis_builder', 'mis_builder_budget', 'account_asset_management',
    ],
    'data': ['views/menus.xml'],
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
