{
    'name': 'KSW - Base Security Extensions',
    'version': '19.0.1.0.0',
    'category': 'Human Resources',
    'summary': 'Extended security groups and rules for Employees and Attendances',
    'author': 'KSW',
    'depends': ['hr', 'hr_attendance', 'om_hr_payroll', 'hr_holidays', 'hr_homeworking', 'contacts', 'utm', 'sale'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'views/sale_order_views.xml',
        'views/hr_employee_views.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
