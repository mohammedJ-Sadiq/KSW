{
    'name': 'KSW Vehicle Custody (HR-12)',
    'version': '19.0.1.2.0',
    'summary': 'Vehicle handover form HR-12: who answers for each vehicle, handed in, received and changed with HR, GM and workshop',
    'description': """
Vehicle Custody
===============
The paper form HR-12, «نموذج تسليم واستلام المركبة», as a request with its route.
Custody (عهدة) is who is personally responsible for a vehicle; it is not the
operating driver dispatch moves day to day.

* Hand In: a department releases a vehicle. Supervisor → HR → GM (one decision,
  on the release request) → Workshop receives and inspects it.
* Receive: a department is given a vehicle. Supervisor names the new driver →
  HR checks his licence → Workshop hands it over. Arrival waits for it.
* Driver Change: one vehicle, two drivers, same department (vacation,
  suspension...). Supervisor → HR → Workshop checkup. No GM.

Approval applies the custody and sends HR (delegation) and Accounting
(financial matters) an information copy. The form is then printed in the HR-12
layout, signed by hand and filed back as a scan; the workshop checkup cannot
complete without it.
    """,
    'author': 'KSW',
    'category': 'Human Resources',
    'depends': ['hr', 'mail', 'KSW_fleet', 'KSW_workshop', 'KSW_fleet_allocation'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/sequence.xml',
        'data/mail_activity_type.xml',
        'data/checklist.xml',
        'report/report_vehicle_custody.xml',
        'views/ksw_vehicle_custody_views.xml',
        'views/integration_views.xml',
        'views/menus.xml',
    ],
    'license': 'LGPL-3',
    'installable': True,
    'application': True,
}
