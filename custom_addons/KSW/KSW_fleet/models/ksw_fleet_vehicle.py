from odoo import api, fields, models


class KswFleetVehicle(models.Model):
    _name = 'ksw.fleet.vehicle'
    _description = 'Vehicle'
    _order = 'name'

    name = fields.Char(string='Fleet No.', required=True)
    vehicle_model = fields.Char(string='Model / Type')
    plate_number = fields.Char(string='Plate Number')
    driver_id = fields.Many2one('hr.employee', string='Default Driver')
    active = fields.Boolean(default=True)

    _name_uniq = models.Constraint(
        'unique(name)',
        'A vehicle with this fleet number already exists.',
    )

    @api.depends('name', 'vehicle_model')
    def _compute_display_name(self):
        for vehicle in self:
            if vehicle.vehicle_model:
                vehicle.display_name = f'{vehicle.name} — {vehicle.vehicle_model}'
            else:
                vehicle.display_name = vehicle.name
