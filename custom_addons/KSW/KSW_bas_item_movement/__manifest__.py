{
    'name': 'KSW BAS Item Movement',
    'version': '19.0.1.0.1',
    'author': 'Mohammed Albadr',
    'category': 'Accounting',
    'summary': 'BAS «الحركة التجارية للأصناف» with cost centres as analytic accounts '
               'and both distance multipliers',
    'description': """
The BAS item-movement report (بيانات وحركات الأصناف ← الحركة التجارية للأصناف),
read live from bas9ss: one row per item line of a delivery note, with

* the vehicle and the driver cost centres resolved to Odoo analytic accounts
  (the plans KSW_bas_gl_import seeded from WREF10);
* «الرد المضاعف» (``cod10.FACTORE``, the customer's current multiplier) and
  «رد الفاتورة» (``vou10.PHRDIS``, the multiplier on the invoice) side by side.

A selection screen first, then an interactive grouped list with pivot and graph.
""",
    'depends': ['analytic', 'KSW_bas_gl_import'],
    'data': [
        'security/ir.model.access.csv',
        'views/item_movement_line_views.xml',
        'wizard/item_movement_wizard_views.xml',
    ],
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
