{
    'name': 'KSW Workshop',
    'version': '19.0.3.0.0',
    'summary': 'Workshop service requests: submission, triage, repair report, and parts inventory',
    'description': """
Workshop Service Requests
==========================
Replaces the Google Form + Google Sheet workflow employees used to request
vehicle workshop service. A request goes Client -> Vehicle Type -> Vehicle
(or "Cash Customer" free text for one-off walk-in work); the workshop
manager triages it (New -> In Progress -> Completed/Rejected); a workshop
technician fills in the repair report (entry/exit, odometer, technician,
spare parts and labor cost) while the request is In Progress.

Workshop Parts Inventory
==========================
A lightweight parts catalog and stock ledger scoped to the workshop. The
manager records stock income (parts received); a technician consumes parts
against an in-progress request via a "Spare Parts Used" table, which
deducts stock live and rolls up into the request's Spare Parts Cost. Free-
text "Repairs & Spare Parts" notes still exist alongside it for one-off
items not worth cataloging.
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
        'views/ksw_workshop_part_views.xml',
        'views/ksw_workshop_part_move_views.xml',
    ],
    'license': 'LGPL-3',
    'installable': True,
    'auto_install': False,
    'application': True,
}
