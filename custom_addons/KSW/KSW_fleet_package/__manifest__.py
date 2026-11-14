{
    'name': 'KSW Vehicle Allocation: Vehicles with Their Drivers',
    'version': '19.0.1.2.0',
    'summary': 'Release vehicles together with their drivers: HR checks, the GM approves, both move at once',
    'description': """
The one transfer that skips the workshop. On a release request a vehicle can
be released "with its driver": HR's review of the request covers the driver's
documents, the GM decides the whole request once, and on approval the vehicle
changes department immediately, its custodian keeps it, and he gets an
Employee Transfer to the same department, applied the same day. HR and
Accounting get the information copy.

Bridges Vehicle Allocation, Vehicle Custody and Employee Transfer; installs by
itself when all three are present.
    """,
    'author': 'KSW',
    'category': 'Human Resources',
    'depends': ['KSW_fleet_allocation', 'KSW_vehicle_custody', 'KSW_employee_transfer'],
    'data': [
        'security/security.xml',
        'views/fleet_package_views.xml',
    ],
    'auto_install': True,
    'installable': True,
    'license': 'LGPL-3',
}
