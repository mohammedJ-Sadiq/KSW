{
    'name': 'KSW Vacation Extension',
    'version': '19.0.1.0.0',
    'author': 'Mohammed Albadr',
    'category': 'Human Resources',
    'summary': 'Extend an approved vacation from its last day, with the '
               'HR fee sheet and the full six-step approval chain',
    'description': """
        Adds a Vacation Extension leave type (تمديد الإجازة) for an employee
        who is already on an approved annual or unpaid vacation and needs more
        time.

        The extension is its own request, anchored to the vacation it
        continues (``x_extended_leave_id``) and starting the day after that
        vacation's last day.  Its days are always unpaid.

        It walks the same six steps as every other KSW vacation:
        Direct Manager, HR, GM Initial, Accounting, GM Final, HR Confirmation.

        At the HR step, HR fills the fees the extension makes due on the
        employee, and their total:
        - Work Permit Fees (رخصة العمل)
        - Iqama Fees (الإقامة)
        - Bank Fees (رسوم البنك)
        - Passport Fees (رسوم الجوازات) — a tick box worth a fixed 120 SAR
        - Violations (مخالفات)
        - Extra Fees (رسوم اضافية)

        The total is recorded on the request; it is not posted to payroll.

        At the Accounting step, Accounting adds a note and any supporting
        documents.

        The extension also takes the return-confirmation gate over from the
        vacation it continues, so the direct manager confirms the real return
        once, on the extension.
    """,
    'depends': [
        'KSW_annual_leave',
        'KSW_unpaid_leave',
        'KSW_payroll',
    ],
    'data': [
        'data/leave_type_data.xml',
        'views/hr_leave_type_views.xml',
        'views/hr_leave_views.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
