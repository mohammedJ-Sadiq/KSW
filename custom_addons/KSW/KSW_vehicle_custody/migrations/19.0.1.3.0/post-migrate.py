from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """19.0.1.3.0: each HR-12 records the workshop that handles it, so workshop
    staff see only their own (the Nabiya manager saw, and could complete, a
    Tabuk form). Fill it on existing forms and re-sync their to-dos."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    Form = env['ksw.vehicle.custody.request']
    Workshop = env['ksw.workshop']
    for form in Form.search([('workshop_id', '=', False)]):
        workshop = form.workshop_request_id.workshop_id
        if not workshop and form.kind == 'receive':
            hand_in = Form.search([('vehicle_id', '=', form.vehicle_id.id), ('kind', '=', 'hand_in'),
                                   ('workshop_request_id', '!=', False)], limit=1)
            workshop = hand_in.workshop_request_id.workshop_id or Workshop._for_department(form.department_id)
        form.write({'workshop_id': workshop.id})
    Form.search([('state', 'in', ('draft', 'hr', 'workshop', 'done'))])._sync_activities()
