{
    'name': 'KSW Stock Movement',
    'version': '19.0.1.0.0',
    'author': 'Mohammed Albadr',
    'category': 'Inventory/Inventory',
    'summary': 'Stock movement report: pick stocks, items and a period, then a '
               'grouped list of every movement with opening, in, out and closing',
    'description': """
The Odoo counterpart of the BAS Item Movement screen, read from Odoo's own
stock moves (Inventory > Reporting > Stock Movement):

* a selection screen first: warehouses or particular stock locations, items,
  item categories, partners, operation types and a period, done moves only or
  also those not yet validated;
* then an interactive grouped list (pivot and graph alongside) with one row per
  side of each movement that touches the chosen stocks, In / Out / Net, and an
  opening-balance row per item and location, so grouping by item shows
  opening + in - out = closing;
* deliveries show whether their sale has been invoiced yet.

Depends on stock only, so it runs wherever Inventory does.
""",
    'depends': ['stock'],
    'data': [
        'security/ir.model.access.csv',
        'views/stock_movement_line_views.xml',
        'wizard/stock_movement_wizard_views.xml',
    ],
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
