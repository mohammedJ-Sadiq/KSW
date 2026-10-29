{
    'name': 'KSW Stock Movement: Water Delivery',
    'version': '19.0.1.2.0',
    'author': 'Mohammed Albadr',
    'category': 'Inventory/Inventory',
    'summary': 'Dispatcher and driver on the Stock Movement report',
    'description': """
Bridge between KSW_stock_movement and KSW_water_delivery: each row of a water
delivery note shows who dispatched it and which of our drivers delivered it.
Installs itself wherever both modules are, so the report keeps depending on
stock only.
""",
    'depends': ['KSW_stock_movement', 'KSW_water_delivery'],
    'data': ['views/stock_movement_views.xml'],
    'installable': True,
    'auto_install': True,
    'license': 'LGPL-3',
}
