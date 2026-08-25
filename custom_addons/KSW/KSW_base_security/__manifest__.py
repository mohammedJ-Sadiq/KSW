{
    'name': 'KSW - Base Security Extensions',
    'version': '19.0.1.2.0',
    'category': 'Human Resources',
    'summary': 'Extended security groups and rules for Employees and Attendances',
    'author': 'KSW',
    'depends': ['hr', 'hr_attendance', 'om_hr_payroll', 'hr_holidays', 'hr_homeworking', 'contacts', 'utm', 'sale'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'views/sale_order_views.xml',
        'views/hr_department_views.xml',
        'views/hr_employee_views.xml',
        'views/res_users_views.xml',
    ],
    'assets': {
        'web.assets_web': [
            'KSW_base_security/static/src/css/rtl.css',
        ],
    },
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
