{
    'name': 'KSW Payroll & Commissions in the Ledger',
    'version': '19.0.1.7.0',
    'summary': 'Posts the payslip batch and the paid commission month as '
               'Odoo journal entries, booked the way BAS books them.',
    'description': """
For the parallel run (Nov 2026): what HR confirms in payroll and what the
accountant marks paid in commissions also lands in Odoo's own ledger, so the
month can be compared with BAS account by account.

Kept apart from the BAS voucher exports on purpose: its own journals
(SAL, COMM), its own fields (Ledger Entry), and it posts nothing dated before
the start date set in Settings, so a month already exported and keyed into
BAS is never booked twice. A posting that fails never blocks Mark As Done or
Mark Paid; it is written on the record for the accountant to fix and post.
""",
    'author': 'KSW',
    'category': 'Accounting',
    'depends': ['account', 'KSW_commissions'],
    'data': [
        'security/ir.model.access.csv',
        'views/payroll_posting_views.xml',
        'views/payroll_ledger_views.xml',
    ],
    'post_init_hook': 'post_init_hook',
    'license': 'LGPL-3',
}
