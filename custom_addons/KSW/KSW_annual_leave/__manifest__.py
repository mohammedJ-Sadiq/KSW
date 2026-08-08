{
    'name': 'KSW Annual Leave',
    'version': '19.0.1.6.0',
    'author': 'Mohammed Albadr',
    'category': 'Human Resources',
    'summary': 'Auto-computed annual leave allocation dashboard',
    'description': """
        Automatically calculates each employee's annual leave entitlement
        from their joining date to today using daily proration:
        - 21 days/year for the first 5 years
        - 30 days/year after 5 years
        Subtracts approved leaves taken and shows the remaining balance.
        Records are auto-created for all employees and refreshed daily
        by a scheduled action.

        Duration for annual-leave requests is computed as calendar days
        (including weekends) per Saudi labor law.
    """,
    'depends': [
        'hr_holidays',
        'KSW_attendance_leave',
        # For the Manager Assistant delegation (res.users.x_assistant_ids
        # and group_manager_assistant) referenced by the record rules in
        # security/security.xml. KSW_base_security has no KSW dependency
        # of its own, so this introduces no cycle.
        'KSW_base_security',
    ],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'views/hr_leave_type_views.xml',
        'views/hr_leave_views.xml',
        'views/annual_leave_views.xml',
        'wizard/absent_days_wizard_views.xml',
        'wizard/opening_balance_wizard_views.xml',
        'wizard/gm_return_approver_wizard_views.xml',
        'wizard/ksw_leave_attendance_wizard_views.xml',
        'wizard/ksw_hr_confirm_signature_wizard_views.xml',
        'data/leave_return_steps.xml',
        'data/cron.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
