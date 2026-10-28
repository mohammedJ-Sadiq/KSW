{
    'name': 'KSW Customer Grace Period',
    'version': '19.0.1.0.0',
    'author': 'Mohammed Albadr',
    'category': 'Accounting',
    'summary': 'Per-customer grace days before an overdue invoice counts '
               'as unpaid for financial risk (credit stop)',
    'description': """
OCA account_financial_risk counts an invoice as unpaid once it is past its
due date plus ONE company-wide Maturity Margin. This module gives each
customer its own grace days, prefilled from BAS (LIMT_DAYS - INVDAYS), so
sale_financial_risk stops each customer on his own day.
""",
    'depends': ['account_financial_risk', 'sale_financial_risk', 'KSW_ext_sync'],
    'data': [
        'views/res_partner_views.xml',
    ],
    'post_init_hook': '_post_init_hook',
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
