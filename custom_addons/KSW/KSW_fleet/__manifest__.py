{
    'name': 'KSW Fleet',
    'version': '19.0.1.0.0',
    'summary': 'Vehicle master data shared across KSW modules',
    'description': """
Vehicle Master Data
====================
Minimal, standalone vehicle reference (fleet number, model/nickname, plate,
default driver). Intentionally has no workflow of its own — it exists so
KSW_workshop (and future asset-management modules) can reference a single
list of vehicles instead of each keeping its own free-text field.
    """,
    'author': 'KSW',
    'category': 'Human Resources',
    'depends': [
        'hr',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/ksw_fleet_vehicle_views.xml',
    ],
    'license': 'LGPL-3',
    'installable': True,
    'auto_install': False,
    'application': False,
}
