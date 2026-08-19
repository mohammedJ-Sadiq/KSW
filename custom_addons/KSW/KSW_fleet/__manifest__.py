{
    'name': 'KSW Fleet',
    'version': '19.0.2.0.0',
    'summary': 'Vehicle master data shared across KSW modules',
    'description': """
Vehicle Master Data
====================
Vehicle reference (fleet number, type, plate, brand/model/year, default
driver) owned by a client (res.partner) — the company's own fleet is just
one client among others. New vehicles from non-managers land in Draft until
a fleet/HR manager confirms them. Exists so KSW_workshop (and future
asset-management modules) can reference a single list of vehicles instead of
each keeping its own free-text field.
    """,
    'author': 'KSW',
    'category': 'Human Resources',
    'depends': [
        'hr',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/ksw_fleet_vehicle_views.xml',
        'data/ksw_fleet_vehicle_data.xml',
    ],
    'license': 'LGPL-3',
    'installable': True,
    'auto_install': False,
    'application': False,
}
