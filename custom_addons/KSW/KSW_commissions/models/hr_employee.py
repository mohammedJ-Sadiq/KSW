from odoo import fields, models


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    # ``hr.employee`` custom fields that don't exist on hr.employee.public
    # MUST declare ``groups='hr.group_hr_user'`` (AGENTS.md gotcha) to
    # avoid AccessError on prefetch for non-HR users.
    x_site_id = fields.Many2one(
        'ksw.site', string='Work Site',
        groups='hr.group_hr_user,base.group_system',
        help='Site assignment used by the KSW driver-commission '
             'sub-form. Site change mid-month: the driver line is '
             'recorded on the month-end site only.',
    )
    x_commission_import_name = fields.Char(
        string='Commission Import Name',
        groups='hr.group_hr_user,base.group_system',
        help='Name exactly as it appears in the accountant\'s monthly '
             'Sales / Collection Excel files (column "البائع" / '
             '"مندوب التحصيل"). Used by the Excel import wizard to '
             'auto-match rows to this employee. Leave blank to fall back '
             'to the employee\'s regular name.',
    )
    x_bas_driver_cost_center = fields.Char(
        string='BAS Driver Cost Center',
        groups='hr.group_hr_user,base.group_system',
        help='Exact "مركز تكلفة الموظف" value for this driver in BAS '
             '(e.g. "WAHAB JAN1387"). Used by the driver-commission '
             '"Pull from BAS" button to match BAS trip rows '
             '(vou10.COST_CENTER2) to this employee. The BAS label often '
             'differs from the employee\'s Odoo name, so this must be set '
             'explicitly for the pull to find the driver\'s loads.',
    )

    # No write() hook any more. It used to pre-create an empty commission
    # sheet for every employee flagged x_is_attendance_sheet. Since
    # 19.0.3.0.0 there is nothing to pre-create: an employee appears in the
    # payment register precisely when somebody records a pay entry for him,
    # so blank per-employee documents no longer exist to be created.

