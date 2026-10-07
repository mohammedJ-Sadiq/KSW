{
    'name': 'KSW Workshop Stock',
    'version': '19.0.1.0.0',
    'author': 'Mohammed Albadr',
    'category': 'Inventory/Inventory',
    'summary': 'Workshop spare parts are issued from real stock, charged to the vehicle cost centre',
    'description': """
Bridge between KSW_workshop and KSW_inventory (installs itself when both are).

Spare parts on a repair report become real stock items (tyres bought on a
purchase order, for instance). Completing the repair issues them from the
workshop's warehouse through that warehouse's Stock Issue operation:
Dr the item class's issue account (tyres 3102020002) with the vehicle's BAS
cost centre / Cr the stock account (1206010003), at weighted average cost.
Completion is refused while stock is short or the vehicle has no cost centre.

The pass-through item list of KSW_workshop is hidden: it was never used in
production (0 items, 0 lines on KSWCO, 2026-10-06).
""",
    'depends': ['KSW_workshop', 'KSW_inventory'],
    'data': [
        'views/workshop_views.xml',
    ],
    'post_init_hook': 'post_init_hook',
    'auto_install': True,
    'installable': True,
    'license': 'LGPL-3',
}
