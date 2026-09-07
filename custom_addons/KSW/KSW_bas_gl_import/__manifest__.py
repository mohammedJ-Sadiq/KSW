{
    'name': 'KSW BAS GL Import',
    'version': '19.0.1.0.0',
    'author': 'Mohammed Albadr',
    'category': 'Accounting',
    'summary': 'Import the BAS (bas9ss) chart of accounts and general ledger into Odoo',
    'description': """
Reads BAS's own double-entry journal (``vou10``) and chart (``COD10``) and
reproduces them as Odoo ``account.move`` / ``account.account`` records.

Deliberately reads BAS **directly** rather than the ``ksw.bas.*`` mirrors in
KSW_ext_sync: those mirrors are built for commissions and are lossy for GL
purposes (no debit/credit indicator, tax always 0).
""",
    'depends': ['account', 'KSW_ext_sync'],
    'data': [
        'security/ir.model.access.csv',
        'views/bas_gl_import_views.xml',
    ],
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
