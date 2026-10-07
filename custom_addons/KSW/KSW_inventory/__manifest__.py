{
    'name': 'KSW Inventory',
    'version': '19.0.1.0.0',
    'author': 'Mohammed Albadr',
    'category': 'Inventory/Inventory',
    'summary': 'BAS warehouses and item classes, perpetual stock valuation, stock issues to an account and cost centre',
    'description': """
Inventory set up the way BAS9 runs it, to the standard of SAP MM / Oracle
Inventory / Odoo Enterprise:

* Warehouses come from BAS (WSHOW, company 10): 21 warehouses and 5 showrooms,
  keyed by their BAS code. Create-only: after the seed Odoo is the master.
* Item classes mirror BAS's stock accounts: tyres 1206010003, desalination
  spare parts 1206010002, membranes 1206010001. Weighted average cost (BAS
  ICTYPE 6) and perpetual valuation, so a vendor bill posts Dr stock / Cr
  supplier like BAS 006.
* Stock Issue (صرف مخزون, BAS 101): one operation per warehouse. Posting at
  validation Dr issue account / Cr stock account. The issue account defaults
  from the item class and can be overridden per issue (BAS charges tyres to a
  driver's own 1207 account). A cost centre is required on every issue.
* Opening stock loads BAS's register (ITMW10/ITMS10) per warehouse at BAS unit
  cost without posting anything.
""",
    'depends': ['stock_account', 'purchase_stock', 'stock_analytic', 'KSW_bas_gl_import'],
    'data': [
        'security/ir.model.access.csv',
        'data/stock_data.xml',
        'views/stock_views.xml',
        'wizard/bas_inventory_setup_views.xml',
        'views/menus.xml',
    ],
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
