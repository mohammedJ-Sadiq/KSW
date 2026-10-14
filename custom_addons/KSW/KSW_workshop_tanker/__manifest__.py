{
    'name': 'KSW Workshop Tankers',
    'version': '19.0.1.4.0',
    'author': 'Mohammed Albadr',
    'category': 'Human Resources/Fleet',
    'summary': "Workshop vehicle picker follows the supervisor's tanker groups "
               "and the drivers BAS puts on each truck",
    'description': """
Bridge between KSW_workshop, KSW_water_delivery and KSW_commissions (installs
itself when all three are).

* Each of our own vehicles carries the driver BAS says is driving it: the
  driver with the most water sales (vou10 FTYPE 600) on that truck's cost
  centre over the last 14 days. A system administrator refreshes it with
  "Update Drivers from BAS" on the vehicle list.
* On a workshop request, a supervisor's vehicle picker offers only the trucks
  in his tanker groups and the trucks driven by his subordinates. Picking one
  fills in its driver. Choosing someone else is allowed, and the request then
  says so to everyone who opens it.
""",
    'depends': ['KSW_workshop', 'KSW_water_delivery', 'KSW_commissions'],
    'data': [
        'views/fleet_vehicle_views.xml',
        'views/workshop_request_views.xml',
    ],
    'auto_install': True,
    'installable': True,
    'license': 'LGPL-3',
}
