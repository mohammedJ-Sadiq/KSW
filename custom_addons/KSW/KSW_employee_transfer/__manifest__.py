{
    'name': 'KSW Employee Transfer',
    'version': '19.0.1.0.0',
    'summary': 'Move an employee to another department: requested, checked by HR, approved by the GM, applied on its date',
    'description': """
Employee Transfer
=================
A department manager requests moving an employee to another department; HR
checks it, the GM approves it, and it takes effect on its effective date (any
day) as a new contract version, so payroll, attendance and commissions keep
reading the old department for the days before.

An employee who still answers for vehicles (custody, KSW_fleet) cannot be
moved until they are handed in, unless he moves together with them (a
vehicle-with-driver transfer from Vehicle Allocation).
    """,
    'author': 'KSW',
    'category': 'Human Resources',
    'depends': ['hr', 'mail', 'KSW_fleet'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/sequence.xml',
        'data/cron.xml',
        'views/ksw_employee_transfer_views.xml',
    ],
    'license': 'LGPL-3',
    'installable': True,
    'application': True,
}
