{
    'name': 'KSW Stock Movement: Client Multiplier',
    'version': '19.0.1.0.0',
    'author': 'Mohammed Albadr',
    'category': 'Inventory/Inventory',
    'summary': '«الرد المضاعف» on the Stock Movement report',
    'description': """
Bridge between KSW_stock_movement and KSW_client_multiplier: each row of the
Stock Movement report shows the customer's multiplier in force on the day of
the movement (from the dated history, so a later change never rewrites an old
row), summed in the group totals like the BAS Item Movement. Installs itself
wherever both modules are, so the report itself keeps depending on stock only.
""",
    'depends': ['KSW_stock_movement', 'KSW_client_multiplier'],
    'data': [
        'views/stock_movement_views.xml',
    ],
    'installable': True,
    'auto_install': True,
    'license': 'LGPL-3',
}
