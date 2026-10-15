from odoo import api, fields, models


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    # The trucks this supervisor may pick on a workshop request, edited
    # directly: the automatic list (tanker groups + what his people drive)
    # comes in pre-filled, and whatever the administrator removes or adds is
    # kept in the two override lists below, so it survives the next BAS
    # driver refresh. Gated like x_water_tanker_group_ids: who answers for
    # which truck is the Employees Administrator's decision.
    x_workshop_vehicle_ids = fields.Many2many(
        'ksw.fleet.vehicle', string='Workshop Vehicles',
        compute='_compute_workshop_vehicle_ids', inverse='_inverse_workshop_vehicle_ids',
        domain="[('x_is_own_vehicle', '=', True), ('vehicle_type', '!=', 'tank')]",
        groups='hr.group_hr_manager',
        help='The trucks this supervisor can pick on a workshop request. Remove '
             'or add trucks here; the change is kept even when BAS later puts '
             'a different driver on the truck.')
    x_workshop_vehicle_added_ids = fields.Many2many(
        'ksw.fleet.vehicle', 'ksw_workshop_supervisor_vehicle_added_rel',
        'employee_id', 'vehicle_id', string='Added by Hand',
        groups='hr.group_hr_manager')
    x_workshop_vehicle_removed_ids = fields.Many2many(
        'ksw.fleet.vehicle', 'ksw_workshop_supervisor_vehicle_removed_rel',
        'employee_id', 'vehicle_id', string='Removed by Hand',
        groups='hr.group_hr_manager',
        help='Trucks taken off this supervisor although his tanker groups or '
             'his drivers would give them to him. Add one back in Workshop '
             'Vehicles to undo.')

    @api.depends('x_workshop_vehicle_added_ids', 'x_workshop_vehicle_removed_ids',
                 'x_water_tanker_group_ids')
    def _compute_workshop_vehicle_ids(self):
        Vehicle = self.env['ksw.fleet.vehicle']
        for employee in self:
            ids = Vehicle._workshop_scope_for_employees(employee) if employee.id else set()
            employee.x_workshop_vehicle_ids = [(6, 0, list(ids))]

    def _inverse_workshop_vehicle_ids(self):
        Vehicle = self.env['ksw.fleet.vehicle']
        for employee in self:
            auto = Vehicle._workshop_auto_scope(employee)
            wanted = set(employee.x_workshop_vehicle_ids.ids)
            employee.x_workshop_vehicle_added_ids = [(6, 0, list(wanted - auto))]
            employee.x_workshop_vehicle_removed_ids = [(6, 0, list(auto - wanted))]
