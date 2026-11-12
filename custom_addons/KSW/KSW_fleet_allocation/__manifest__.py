{
    'name': 'KSW Vehicle Allocation',
    'version': '19.0.1.6.1',
    'summary': 'Move vehicles between departments: release and addition requests decided by the GM',
    'description': """
Vehicle Allocation
==================
Every company vehicle belongs to a department (Supply Chain, Tabuk, the two
Isuzu branches, Equipment Rental). A department raises a Release Request for
vehicles it no longer needs, or an Additional Vehicles request for a type and
a quantity. Nothing moves between departments by itself: the GM decides each
line.

* A released vehicle is transferred to another department (usually one that
  asked for one), kept where it is, or sent to the workshop for a checkup and
  parked in the Holding pool.
* An addition is satisfied internally (vehicles from the pool, from a release,
  or taken from another department), externally (purchase / rent), or refused,
  in any mix: a request for three can be two internal and one external.

A transfer only completes when the receiving department confirms it received
the vehicle, so the record never runs ahead of the road. Every move is kept as
history on the vehicle.
    """,
    'author': 'KSW',
    'category': 'Human Resources',
    'depends': [
        'hr',
        'mail',
        'KSW_fleet',
        # Sending a released vehicle to the workshop raises a checkup request.
        'KSW_workshop',
    ],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/sequence.xml',
        'data/mail_activity_type.xml',
        'wizard/ksw_fleet_release_decision_wizard_views.xml',
        'wizard/ksw_fleet_allocate_wizard_views.xml',
        'wizard/ksw_fleet_receive_wizard_views.xml',
        'views/ksw_fleet_move_views.xml',
        'views/ksw_fleet_request_views.xml',
        'views/ksw_fleet_vehicle_views.xml',
        'views/hr_department_views.xml',
        'views/res_users_views.xml',
        'views/overview_views.xml',
        'views/menus.xml',
    ],
    'post_init_hook': '_post_init_hook',
    'license': 'LGPL-3',
    'installable': True,
    'application': True,
}
