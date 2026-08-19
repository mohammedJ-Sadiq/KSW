from odoo import _, api, fields, models
from odoo.exceptions import UserError


class KswFleetVehicle(models.Model):
    _name = 'ksw.fleet.vehicle'
    _description = 'Vehicle'
    _order = 'name'

    name = fields.Char(string='Fleet No.', required=True)
    client_id = fields.Many2one(
        'res.partner', string='Client', required=True,
        default=lambda self: self.env.company.partner_id,
    )
    vehicle_type = fields.Selection([
        ('trailer', 'Trailer'),
        ('isuzu', 'Isuzu'),
        ('tank', 'Tank'),
        ('other', 'Other'),
    ], string='Vehicle Type')

    tank_number = fields.Char(string='Tank Number')
    plate_number = fields.Char(string='Plate Number')

    driver_id = fields.Many2one('hr.employee', string='Default Driver')
    driver_name = fields.Char(
        string='Driver Name', compute='_compute_driver_name', store=True, readonly=False,
    )

    brand = fields.Char()
    model = fields.Char()
    year = fields.Integer()

    state = fields.Selection([
        ('draft', 'Draft'),
        ('confirmed', 'Confirmed'),
    ], default='confirmed', copy=False, required=True)

    active = fields.Boolean(default=True)

    _name_uniq = models.Constraint(
        'unique(client_id, name)',
        'A vehicle with this fleet number already exists for this client.',
    )

    @api.depends('name', 'brand', 'model')
    def _compute_display_name(self):
        for vehicle in self:
            label = ' '.join(part for part in (vehicle.brand, vehicle.model) if part)
            vehicle.display_name = f'{vehicle.name} — {label}' if label else vehicle.name

    @api.depends('driver_id')
    def _compute_driver_name(self):
        for vehicle in self:
            if vehicle.driver_id:
                vehicle.driver_name = vehicle.driver_id.name
            # else: leave whatever was typed manually untouched

    @api.model_create_multi
    def create(self, vals_list):
        is_manager = self.env.su or self.env.user.has_group('hr.group_hr_user')
        for vals in vals_list:
            if not is_manager:
                vals['state'] = 'draft'
            elif not vals.get('state'):
                vals['state'] = 'confirmed'
        return super().create(vals_list)

    def action_confirm(self):
        if not self.env.su and not self.env.user.has_group('hr.group_hr_user'):
            raise UserError(_('Only a fleet/workshop manager can confirm a draft vehicle.'))
        for vehicle in self:
            if vehicle.state != 'draft':
                raise UserError(_('Only draft vehicles can be confirmed.'))
        self.write({'state': 'confirmed'})
