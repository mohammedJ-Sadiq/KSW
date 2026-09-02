{
    'name': 'KSW End of Service Leave',
    'version': '19.0.1.2.0',
    'author': 'Mohammed Albadr',
    'category': 'Human Resources',
    'summary': 'End-of-Service request leave type with 6-step approval and EOS payslip',
    'description': """
        Adds an End of Service Request leave type (طلب مكافأة نهاية الخدمة) that
        mirrors the annual-leave 6-step approval chain without requiring an
        allocation balance.

        At the HR Approval step the HR team fills:
        - Unpaid vacation days (reduces the service period before computing Art. 84/85)
        - Termination Reason (Article 84 — Termination, or Article 85 — Resignation)
        - Previous Payments (deducted from the EOS payslip)
        - Notice Pay — Deduction (deducted from the EOS payslip)

        At GM Final Approval a combined payslip is created that includes the
        employee's regular final-month salary plus the EOS-specific amounts.
    """,
    'depends': [
        'KSW_annual_leave',
        'KSW_payroll',
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/eos_salary_rules.xml',
        'data/leave_type_data.xml',
        'views/hr_leave_type_views.xml',
        'views/hr_leave_views.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
