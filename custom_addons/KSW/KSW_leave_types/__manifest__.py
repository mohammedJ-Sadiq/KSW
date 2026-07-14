{
    'name': 'KSW Leave Types',
    'version': '19.0.1.0.0',
    'category': 'Human Resources',
    'summary': 'Saudi Labour Law leave types with automatic allocation/accrual',
    'author': 'Mohammed Albadr',
    'license': 'LGPL-3',
    'depends': [
        'KSW_leave_approval',
        'KSW_annual_leave',
    ],
    'data': [
        'data/leave_type_data.xml',
        'data/accrual_plan_data.xml',
        'data/cron.xml',
    ],
    'post_init_hook': '_post_init_hook',
    'installable': True,
    'auto_install': False,
    'application': False,
}
