{
    'name': 'KSW - Mobile Layout',
    'version': '19.0.1.2.0',
    'author': 'Mohammed Albadr',
    'summary': 'Phone-only layout for the most-used KSW screens; desktop unchanged',
    'description': """
Phone layout rules (pattern taken from SAP Fiori / Oracle Redwood / Odoo Enterprise):

1. Lists you browse or approve open as cards on a phone. Each action gets an
   explicit list-then-kanban view order owned by THIS module, so desktop still
   opens the list and upgrading the owning module does not undo it.
2. Tables you type into keep only their priority columns on a phone: any list
   column with class="ksw_m_hide" is dropped below the small-screen breakpoint
   (static/src/js/list_renderer_patch.js). Header, cells and totals stay aligned.
3. Compact record header and readable long text, phone-width CSS only.
    """,
    'license': 'LGPL-3',
    'depends': [
        'web',
        'hr_holidays',
        'KSW_attendance_sheet',
        'KSW_annual_leave',
        'KSW_deduction',
        'KSW_commissions',
        'KSW_payroll',
        'KSW_leave_types',
        'hr_attendance',
    ],
    'data': [
        'views/attendance_sheet_views.xml',
        'views/hr_leave_views.xml',
        'views/ksw_deduction_views.xml',
        'views/ksw_pay_batch_views.xml',
        'views/hr_attendance_views.xml',
        'views/ksw_commissions_views.xml',
        'views/ksw_payroll_views.xml',
        'views/hr_leave_allocation_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'KSW_mobile_ui/static/src/js/list_renderer_patch.js',
            'KSW_mobile_ui/static/src/scss/mobile.scss',
        ],
    },
    'installable': True,
    'auto_install': False,
    'application': False,
}
