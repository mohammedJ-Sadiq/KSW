{
    'name': 'KSW BAS External Sync',
    'version': '19.0.1.0.0',
    'category': 'Custom',
    'summary': 'Read-only sync from BAS (bas9ss) SQL Server accounting system',
    'depends': ['base', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'data/cron.xml',
        'views/bas_account_views.xml',
        'views/bas_customer_views.xml',
        'views/bas_invoice_views.xml',
        'views/bas_payment_views.xml',
        'views/menus.xml',
    ],
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
