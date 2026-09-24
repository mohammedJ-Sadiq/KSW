{
    'name': 'KSW Water Delivery Notes',
    'version': '19.0.1.5.0',
    'summary': 'Issue the water delivery note at the client, online or off',
    'description': """
Water Delivery Notes
====================
KSW delivers sweet water by tanker. Today the delivery note is printed by BAS9,
signed on paper at the customer, carried back to accounting and accumulated
until month end, when one tax invoice is raised against the whole pile.

This module moves the *note* into Odoo. The driver creates the delivery on his
phone at the customer, the customer signs on the screen, and Odoo stores the
signed PDF against the record immediately (core `stock.picking._attach_sign`).

It deliberately builds on the native chain — `sale.order` -> `stock.picking` ->
invoice on delivered quantity — rather than a shortcut, so that when invoicing
itself moves off BAS9 nothing has to be rebuilt. Until that day, BAS9 still
issues the tax invoice and keeps its ZATCA hash chain; Odoo just hands
accounting the finished list.
    """,
    'author': 'Mohammed Albadr',
    'category': 'Inventory/Delivery',
    'depends': [
        'sale_management',
        'sale_stock',
        'stock',
        'account',
        'hr',
        'KSW_fleet',
        # Rates are seeded from what BAS actually charged: there is no
        # pricelist table in BAS, so the connector is how we read str10.
        'KSW_ext_sync',
        # partner_latitude / partner_longitude, for the client locations the
        # drivers' notes teach us.
        'base_geolocalize',
    ],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/ksw_water_delivery_data.xml',
        'data/ksw_water_app_templates.xml',
        'report/report_water_delivery_note.xml',
        'views/ksw_water_rate_views.xml',
        'views/ksw_water_client_branch_views.xml',
        'views/hr_employee_views.xml',
        'views/stock_picking_views.xml',
        'views/ksw_fleet_vehicle_views.xml',
        'views/ksw_water_delivery_wizard_views.xml',
        'views/ksw_water_capture_views.xml',
        'views/ksw_water_delivery_menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'KSW_water_delivery/static/src/js/geolocation_widget.js',
            'KSW_water_delivery/static/src/js/driver_kiosk.js',
            'KSW_water_delivery/static/src/scss/driver_kiosk.scss',
            'KSW_water_delivery/static/src/xml/geolocation_widget.xml',
        ],
    },
    'installable': True,
    'auto_install': False,
    'application': False,
    'license': 'LGPL-3',
}
