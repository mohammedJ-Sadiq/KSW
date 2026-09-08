{
    'name': 'KSW Workshop',
    'version': '19.0.8.0.0',
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

Clients offered on a request are the ones the workshop manager registered
under Configuration -> Clients (a role assignment on res.partner, not a flag).
Spare parts are recorded as a pass-through list on the repair report: items are
identified by their purchase-invoice item number, issued as they are entered,
with no stock balance kept — the value is the issue log. The form also shows
what this vehicle, and this driver under this client, already had done this
year.

Note: a full parts ledger with on-hand quantities + a multi-location extension
was built and then postponed by explicit request (2026-08-20) — removed from
the code, design notes kept in the KSW-Brain vault. The pass-through list above
is the lighter thing asked for instead, not a resumption of that design.
    """,
    'author': 'KSW',
    'category': 'Human Resources',
    'depends': [
        'hr',
        'mail',
        # customer_rank — the client-role marker used by client_id's domain.
        # Reachable transitively (KSW_base_security -> sale -> account, and
        # KSW_fleet -> account), but declared so the reliance is honest.
        'account',
        'KSW_base_security',
        'KSW_fleet',
    ],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/sequence.xml',
        'views/ksw_workshop_request_views.xml',
        'views/ksw_workshop_client_views.xml',
        'views/ksw_workshop_part_views.xml',
        'views/ksw_fleet_vehicle_views.xml',
        'views/ksw_fleet_vehicle_menu.xml',
    ],
    'post_init_hook': '_post_init_hook',
    'license': 'LGPL-3',
    'installable': True,
    'auto_install': False,
    'application': True,
}
