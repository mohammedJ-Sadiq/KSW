{
    'name': 'KSW Commissions: Client Multiplier',
    'version': '19.0.1.1.0',
    'author': 'Mohammed Albadr',
    'category': 'Human Resources',
    'summary': 'Driver trips weighted on the Odoo «الرد المضاعف», back-dated changes re-weighed',
    'description': """
Bridge between KSW_commissions and KSW_client_multiplier.

* From 1 Oct 2026 (``ksw_commissions.odoo_multiplier_from``) BAS supplies only
  the loads (driver, customer, day); each load is weighted with the client's
  «الرد المضاعف» from the Odoo history, in force on the day of the load.
* When the GM approves a change whose effective date is in the past, the
  Driver Trips already recorded for those days are re-weighed: a month not yet
  paid is corrected in place (an approved month keeps its approval and its
  register line is re-derived); a paid month is left as paid and the
  difference goes to the driver's next open month as «Driver Trips Remainder».
* Trip Details (button on each driver-trips row and in the tier explanation):
  every BAS load behind the weighted quantity, with the multiplier used for
  it and where that multiplier comes from, adding up to the line.
""",
    'depends': ['KSW_commissions', 'KSW_client_multiplier'],
    'data': [
        'security/ir.model.access.csv',
        'views/trip_detail_views.xml',
    ],
    'installable': True,
    'auto_install': True,
    'license': 'LGPL-3',
}
