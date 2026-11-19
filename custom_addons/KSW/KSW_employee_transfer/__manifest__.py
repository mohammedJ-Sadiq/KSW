{
    'name': 'KSW Employee Transfer',
    'version': '19.0.2.0.1',
    'summary': 'Departments release or request employees; the GM decides; HR carries it out',
    'description': """
Employee Transfer
=================
The same pattern as Vehicle Allocation. A department releases employees it no
longer needs, or asks for employees (job and number). Only the GM decides:

* a released employee goes to another department's open request (transfer),
  stays on hold (keeps working, offered to the next request), ends his
  service, or the release is refused;
* a request is filled from released employees, approved for hiring from
  outside, held, or refused.

HR carries out what he decided: completes the transfer (who he answers to in
the new department) which applies on its date as a new contract version, opens
the End of Service, or names the new hires. Nobody moves an employee directly.

An employee who answers for vehicles: his department says, on the release, what
happens to each (another driver, or released to the workshop); the move waits
until they are handed over.
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
        'views/ksw_employee_request_views.xml',
        'views/hr_employee_views.xml',
    ],
    'license': 'LGPL-3',
    'installable': True,
    'application': True,
}
