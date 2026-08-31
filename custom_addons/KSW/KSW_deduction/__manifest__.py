{
    'name': 'KSW Deductions',
    'version': '19.0.1.5.3',
    'summary': 'Manage employee deductions (loans, penalties, advances, etc.)',
    'description': """
Centralized deduction management for KSW payroll.

Categories:
- Borrowed: Employee borrows money with consent (Loan, Salary Advance)
- Company-Paid: Company pays on behalf of employee (Gov Penalty, Internal Penalty)

Loans follow a 5-step approval workflow (DM -> HR -> Accounting -> GM).
Non-loan deductions use instant-apply (draft -> active in one click).

All pending installments are auto-injected as payslip inputs and deducted
via the KSW_DEDUCTIONS salary rule (regular + vacation payslips).
""",
    'author': 'KSW',
    'category': 'Human Resources/Payroll',
    'depends': [
        'hr',
        'hr_holidays',
        'mail',
        'KSW_payroll',
        'KSW_base_security',
    ],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/sequence.xml',
        'data/deduction_type_data.xml',
        'data/salary_rule_deduction.xml',
        'data/loan_return_steps.xml',
        'views/ksw_deduction_type_views.xml',
        # Loaded before the deduction and employee forms: both carry a
        # "Statement of Account" button that resolves this action's xml id
        # at load time via %(...)d, which fails if the record does not
        # exist yet.
        'wizard/ksw_deduction_statement_wizard_views.xml',
        'views/ksw_deduction_views.xml',
        'views/ksw_deduction_line_views.xml',
        'views/hr_employee_views.xml',
        'views/res_config_settings_views.xml',
        'wizard/loan_request_wizard_views.xml',
        'wizard/loan_refuse_wizard_views.xml',
        'wizard/ksw_loan_payment_wizard_views.xml',
        'wizard/ksw_loan_return_approver_wizard_views.xml',
        'report/ksw_deduction_statement_templates.xml',
        'views/menu.xml',
    ],
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
    'post_init_hook': '_post_init_hook',
}



