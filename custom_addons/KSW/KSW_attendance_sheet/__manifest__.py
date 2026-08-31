{
    'name': 'KSW Attendance Sheet',
    'version': '19.0.1.5.0',
    'category': 'Human Resources',
    'sequence': 1,
    'author': 'Mohammed Albadr',
    'summary': 'Monthly attendance sheet for non-biometric employees',
    'description': """
KSW Attendance Sheet
====================
Manages monthly attendance for employees who do not use biometric
punch-in/punch-out.  Their manager reviews each month and marks
absent days; all other workdays default to "present".  hr.attendance
records are created tagged as auto-generated so the payroll pipeline
can consume them identically to biometric records.

The month only reaches payroll once the direct manager presses
"Confirm & Send to Payroll".  Confirmation is refused while the sheet
contradicts approved time off, and an unconfirmed month is read by the
payslip batch as zero attendance.
    """,
    'license': 'LGPL-3',
    'depends': [
        'mail',
        'hr_biometric_attendance',
        # _confirmation_blockers reads hr.leave to refuse a sheet that
        # contradicts approved time off.
        'hr_holidays',
        'KSW_working_schedule',
    ],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'views/hr_employee_views.xml',
        'views/hr_attendance_views.xml',
        'views/attendance_sheet_views.xml',
        'data/cron.xml',
    ],
    'installable': True,
    'auto_install': False,
    'application': False,
}

