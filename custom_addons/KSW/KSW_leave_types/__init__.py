from . import models


def _post_init_hook(env):
    sick_type = env.ref('KSW_leave_types.leave_type_sick', raise_if_not_found=False)
    if sick_type:
        # Remove any accrual-type sick leave allocations from a previous install attempt
        accrual_allocs = env['hr.leave.allocation'].search([
            ('holiday_status_id', '=', sick_type.id),
            ('allocation_type', '=', 'accrual'),
        ])
        accrual_allocs.sudo().unlink()

        # Migrate leave requests from any pre-existing "Sick Time Off" types (no xmlid)
        # to our canonical Sick Leave type so that leaves_taken computes correctly.
        # Uses direct SQL to bypass ORM state constraints on validated leaves.
        old_sick_types = env['hr.leave.type'].sudo().with_context(active_test=False).search([
            ('id', '!=', sick_type.id),
            ('name', 'ilike', 'Sick'),
        ]).filtered(lambda t: not env['ir.model.data'].sudo().search([
            ('model', '=', 'hr.leave.type'), ('res_id', '=', t.id),
        ], limit=1))
        if old_sick_types:
            env.cr.execute(
                "UPDATE hr_leave SET holiday_status_id = %s WHERE holiday_status_id = ANY(%s)",
                (sick_type.id, list(old_sick_types.ids)),
            )
            # Archive orphaned types so employees cannot pick them for new requests
            env.cr.execute(
                "UPDATE hr_leave_type SET active = FALSE WHERE id = ANY(%s)",
                (list(old_sick_types.ids),),
            )

    employees = env['hr.employee'].search([
        ('active', '=', True),
        ('employee_type', '=', 'employee'),
    ])
    employees._create_sick_leave_allocations()
    employees._create_hajj_leave_allocations()
