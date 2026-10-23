{
    'name': 'KSW Client Multiplier',
    'version': '19.0.1.0.0',
    'author': 'Mohammed Albadr',
    'category': 'Inventory/Delivery',
    'summary': '«الرد المضاعف» per client: dated history, changed only by a '
               'Supervisor request approved by the department GM',
    'description': """
Odoo is the master of each client's «الرد المضاعف» (distance multiplier).

* One record per BAS customer account, with a dated history of its multiplier.
  Seeded once from BAS (``cod10.FACTORE``); BAS's current value stays visible
  beside it only to confirm the transfer.
* Nobody edits it. Accounting reads it (Accounting ▸ Customers ▸ Client
  Multipliers); a Water Delivery Supervisor raises a change request with a
  reason and an effective date; the requester's department GM
  (``hr.department.x_effective_gm_id``, else the company default GM) approves
  or refuses. An approved change applies from its effective date only.
* ``ksw.client.multiplier._multiplier_on(codes, day)`` is the one resolver
  every consumer reads.
""",
    'depends': ['mail', 'account', 'KSW_ext_sync', 'KSW_base_security', 'KSW_water_delivery'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/ir_sequence.xml',
        'data/ir_cron.xml',
        'wizard/refuse_wizard_views.xml',
        'views/client_multiplier_views.xml',
        'views/multiplier_request_views.xml',
        'views/menus.xml',
    ],
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
