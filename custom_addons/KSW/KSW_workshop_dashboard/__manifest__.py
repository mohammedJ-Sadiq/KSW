{
    'name': 'KSW Workshop Dashboard',
    'version': '19.0.1.1.0',
    'author': 'Mohammed Albadr',
    'category': 'Human Resources',
    'summary': 'Workshop overview for reviewers: open work and its age, throughput, turnaround, '
               'repeat repairs, vehicles, technicians and cost',
    'description': """
The Workshop app's overview page, for the Reviewer role (and the Manager, who
implies it). Laid out the way SAP's Maintenance Backlog Overview, Oracle
Fusion's Maintenance Management infolets and Odoo's Repairs overview do it:
the open backlog and how old it is, what came in and what went out in the
period, how long a job takes, which vehicles keep coming back, who did the
work and what it cost. Every figure opens the requests it counts.

A bridge: it reuses the KSW ERP Dashboard's period picker, chart and styles,
and installs by itself when both modules are present.
""",
    'depends': ['KSW_workshop', 'KSW_erp_dashboard'],
    'data': [
        'views/workshop_dashboard_views.xml',
        'data/hub_dashboard.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'KSW_workshop_dashboard/static/src/**/*',
        ],
    },
    'installable': True,
    'auto_install': True,
    'license': 'LGPL-3',
}
