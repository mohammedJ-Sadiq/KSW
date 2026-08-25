{
    'name': 'KSW HR Dashboard',
    'version': '19.0.1.0.0',
    'summary': 'HR/Payroll/Attendance/Deduction overview in the Dashboards app',
    'description': """
Adds a "KSW HR Overview" spreadsheet dashboard under the Dashboards app's
Human Resources group, covering:

- Payroll by department
- Pending annual leave approvals by step
- Attendance absence trend
- Outstanding deduction balance

Pure data module -- no new models.
""",
    'author': 'KSW',
    'category': 'Human Resources',
    'depends': [
        'spreadsheet_dashboard',
        'KSW_payroll',
        'KSW_deduction',
    ],
    'data': [
        'data/dashboards.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
