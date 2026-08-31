{
    'name': 'KSW Workshop',
    'version': '19.0.7.0.0',
    'summary': 'Workshop service requests: submission, triage, and repair report',
    'description': """
Workshop Service Requests
==========================
Replaces the Google Form + Google Sheet workflow employees used to request
vehicle workshop service. A request goes Client -> Vehicle Type -> Vehicle
(or "Cash Customer" free text for one-off walk-in work); the workshop
manager triages it (New -> In Progress -> Completed/Rejected); a workshop
technician fills in the repair report (entry/exit, odometer, technician,
spare parts and labor cost) while the request is In Progress.

Note: a parts inventory + multi-location extension was built and then
postponed by explicit request (2026-08-20) — removed from the code, design
notes kept in the KSW-Brain vault for a future resumption.
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
        'views/ksw_fleet_vehicle_views.xml',
        'views/ksw_fleet_vehicle_menu.xml',
    ],
    'license': 'LGPL-3',
    'installable': True,
    'auto_install': False,
    'application': True,
}
