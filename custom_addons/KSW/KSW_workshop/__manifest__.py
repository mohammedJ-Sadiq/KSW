{
    'name': 'KSW Workshop',
    'version': '19.0.1.0.0',
    'summary': 'Workshop service requests: submission, triage, and repair report',
    'description': """
Workshop Service Requests
==========================
Replaces the Google Form + Google Sheet workflow employees used to request
vehicle workshop service. Employees submit a request against a vehicle; the
workshop manager triages it (New -> In Progress -> Completed/Rejected); a
workshop technician fills in the repair report (entry/exit, odometer,
technician, spare parts and labor cost) while the request is In Progress.
    """,
    'author': 'KSW',
    'category': 'Human Resources',
    'depends': [
        'hr',
        'mail',
        'KSW_base_security',
        'KSW_fleet',
    ],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/sequence.xml',
        'views/ksw_workshop_request_views.xml',
        'views/ksw_fleet_vehicle_menu.xml',
    ],
    'license': 'LGPL-3',
    'installable': True,
    'auto_install': False,
    'application': True,
}
