{
    'name': 'KSW ERP Dashboard',
    'version': '19.0.1.0.0',
    'author': 'Mohammed Albadr',
    'category': 'Accounting',
    'summary': 'Accounting and Inventory overview pages: cash, aging, DSO/DPO, P&L, stock value, '
               'dead and slow stock, days of supply, work waiting',
    'description': """
The landing pages of Accounting and Inventory, laid out the way SAP Fiori
overview pages, Oracle Fusion infolets and Odoo Enterprise do it: the money
position, what is owed and overdue, how the period is going, stock value, stock
that does not move, stock about to run out, and the work waiting. Every card
opens the records it counts. Odoo's journal cards and operation cards stay one
click away.
""",
    'depends': ['account', 'stock', 'analytic'],
    'data': [
        'views/dashboard_actions.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'KSW_erp_dashboard/static/src/common/**/*',
            'KSW_erp_dashboard/static/src/finance/**/*',
            'KSW_erp_dashboard/static/src/inventory/**/*',
        ],
    },
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
