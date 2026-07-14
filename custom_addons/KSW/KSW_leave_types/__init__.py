from . import models


def _post_init_hook(env):
    # Remove any accrual-type sick leave allocations created by a previous install attempt
    sick_type = env.ref('KSW_leave_types.leave_type_sick', raise_if_not_found=False)
    if sick_type:
        accrual_allocs = env['hr.leave.allocation'].search([
            ('holiday_status_id', '=', sick_type.id),
            ('allocation_type', '=', 'accrual'),
        ])
        accrual_allocs.sudo().unlink()

    employees = env['hr.employee'].search([
        ('active', '=', True),
        ('employee_type', '=', 'employee'),
    ])
    employees._create_sick_leave_allocations()
    employees._create_hajj_leave_allocations()
