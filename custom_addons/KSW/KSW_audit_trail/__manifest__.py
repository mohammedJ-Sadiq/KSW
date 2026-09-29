{
    'name': 'KSW Audit Trail',
    'version': '19.0.1.0.0',
    'category': 'Human Resources',
    'author': 'Mohammed Albadr',
    'summary': 'Permanent log + chatter for every change to users, '
               'employees, contracts and salary bank accounts',
    'description': """
KSW Audit Trail
===============
Every change to a user (login, active, companies, groups granted or
removed, password changed), an employee, an employee contract/version
(wage, allowances, structure, schedule, dates) or a bank account attached
to an employee is:

* written to a permanent **Audit Log** (who, when, record, field, old value,
  new value) that nobody can edit or delete from the application, readable
  by System Administrators and HR Managers only; and
* posted to the employee's chatter (or the user's contact when there is no
  employee). Values of fields restricted to a group (e.g. wage) show only as
  "changed" in the chatter, never the figures.
    """,
    'license': 'LGPL-3',
    'depends': ['hr', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'views/ksw_audit_log_views.xml',
    ],
    'installable': True,
    'application': False,
}
