from odoo import _, api, fields, models
from odoo.exceptions import UserError

DRIVER_GROUP = 'KSW_water_delivery.group_water_driver'
DISPATCHER_GROUP = 'KSW_water_delivery.group_water_dispatcher'

# What a dispatcher may change on a vehicle from Water Delivery > Tankers. The
# vehicle record itself (fleet no., plate, client, type) belongs to fleet/HR;
# who drives it, out of which branch and how much a trip holds is dispatch's.
_DISPATCH_FIELDS = {'driver_id', 'x_picking_type_id', 'x_capacity_m3', 'x_driver_client_ids'}


class KswFleetVehicle(models.Model):
    _inherit = 'ksw.fleet.vehicle'

    # Which branch's note numbering this tanker writes into. The branch follows
    # the truck rather than the driver: a driver can be lent to another branch
    # for a day, the tanker belongs where it is based. Left empty, the wizard
    # falls back to the module's default operation type, which is how the
    # single-branch pilot runs.
    x_picking_type_id = fields.Many2one(
        'stock.picking.type', string='Delivery Note Type',
        domain="[('x_is_water_delivery', '=', True)]",
        help='Operation type (and therefore branch and note numbering) used '
             'for delivery notes issued from this tanker.',
    )

    # A "trip" is not a fixed volume: a trailer carries 32 m³ and an Isuzu far
    # less, which is why this is a property of the truck rather than a unit of
    # measure. A UoM factor is a global constant; this one varies per vehicle,
    # so the conversion happens when the note is issued.
    x_capacity_m3 = fields.Float(
        string='Trip Volume (m³)', digits='Product Unit',
        help='How many cubic metres one full trip of this tanker delivers. '
             'Used when the driver enters a quantity in trips instead of m³.',
    )

    # The driver's client list (`hr.employee.x_water_client_ids`), shown and
    # edited from the tanker. Dispatch has no HR rights, so the employee form
    # is out of reach for them, and Tankers is already where they decide who
    # drives what. The list belongs to the DRIVER, not the truck: move him to
    # another tanker and his clients go with him. No `groups=` (gotcha #31).
    x_driver_client_ids = fields.Many2many(
        'res.partner', string="Driver's Clients",
        compute='_compute_driver_client_ids', inverse='_inverse_driver_client_ids',
        domain="[('customer_rank', '>', 0)]",
        help="The clients this tanker's driver normally serves. Leave empty "
             "to let him deliver to every client his branch serves.",
    )

    @api.depends('driver_id', 'driver_id.x_water_client_ids')
    def _compute_driver_client_ids(self):
        for vehicle in self:
            vehicle.x_driver_client_ids = vehicle.driver_id.sudo().x_water_client_ids

    def _inverse_driver_client_ids(self):
        if not self.env.su and not (
                self.env.user.has_group(DISPATCHER_GROUP)
                or self.env.user.has_group('KSW_water_delivery.group_water_billing')):
            raise UserError(_("Only a supervisor can change a driver's clients."))
        for vehicle in self:
            if not vehicle.driver_id:
                if vehicle.x_driver_client_ids:
                    raise UserError(_(
                        '%s has no driver. Assign the driver first, then his '
                        'clients.', vehicle.name))
                continue
            # sudo(): authority was checked above, and write() has already
            # limited a non-HR user to the dispatch fields.
            vehicle.driver_id.sudo().x_water_client_ids = vehicle.x_driver_client_ids

    x_tanker_group_ids = fields.Many2many(
        'ksw.water.tanker.group', 'ksw_water_tanker_group_vehicle_rel',
        'vehicle_id', 'group_id', string='Tanker Groups')
    # "Is this tanker in one of MY groups": what Water Delivery > Tankers
    # filters on. A search method rather than an ir.rule, because a rule on
    # ksw.fleet.vehicle would also hide vehicles from a supervisor's Workshop
    # work; this screen is the only place the narrowing belongs.
    x_in_my_tanker_groups = fields.Boolean(
        compute='_compute_in_my_tanker_groups', search='_search_in_my_tanker_groups')

    def _my_tanker_group_vehicle_ids(self):
        """None = no narrowing (administrators); otherwise the tanker ids."""
        user = self.env.user
        if self.env.su or user.has_group('base.group_system'):
            return None
        employee = user.sudo().employee_id
        return employee.x_water_tanker_group_ids.filtered('active').vehicle_ids.ids

    @api.depends_context('uid')
    def _compute_in_my_tanker_groups(self):
        allowed = self._my_tanker_group_vehicle_ids()
        for vehicle in self:
            vehicle.x_in_my_tanker_groups = allowed is None or vehicle.id in allowed

    def _search_in_my_tanker_groups(self, operator, value):
        # Odoo 19 hands a boolean condition over as in/not in (gotcha #24).
        if operator in ('in', 'not in'):
            wanted = (operator == 'in') == any(value)
        elif operator in ('=', '!='):
            wanted = (operator == '=') == bool(value)
        else:
            return NotImplemented
        allowed = self._my_tanker_group_vehicle_ids()
        if allowed is None:
            return [] if wanted else [('id', '=', False)]
        return [('id', 'in' if wanted else 'not in', allowed)]

    # KSW_fleet also holds workshop clients' vehicles; no driver of ours
    # delivers water in those. Searchable, not stored: "is the client one of
    # our companies" has nothing to depend on when a company is added.
    x_is_own_vehicle = fields.Boolean(
        compute='_compute_is_own_vehicle', search='_search_is_own_vehicle')

    def _own_partner_ids(self):
        return self.env['res.company'].sudo().search([]).partner_id.ids

    def _compute_is_own_vehicle(self):
        own = set(self._own_partner_ids())
        for vehicle in self:
            vehicle.x_is_own_vehicle = vehicle.client_id.id in own

    def _search_is_own_vehicle(self, operator, value):
        # Odoo 19 hands a boolean condition over as in/not in (gotcha #24).
        if operator in ('in', 'not in'):
            wanted = (operator == 'in') == any(value)
        elif operator in ('=', '!='):
            wanted = (operator == '=') == bool(value)
        else:
            return NotImplemented
        return [('client_id', 'in' if wanted else 'not in', self._own_partner_ids())]

    def write(self, vals):
        if not self.env.su and not self.env.user.has_group('hr.group_hr_user'):
            # The dispatcher's ACL row is what lets this write through at all;
            # the row cannot say which fields, so this does.
            extra = set(vals) - _DISPATCH_FIELDS
            if extra:
                raise UserError(_(
                    'From Water Delivery you can only change the driver, his '
                    'clients, the branch and the trip volume of a tanker. Ask '
                    'fleet/HR to change anything else.'))
        if self.env.context.get('ksw_water_tanker_assign'):
            self._water_check_team(vals)
        if vals.get('driver_id') and self.env.context.get('ksw_water_tanker_assign'):
            self._water_check_driver(vals['driver_id'])
        res = super().write(vals)
        if vals.get('driver_id') and self.env.context.get('ksw_water_tanker_assign'):
            self._water_move_driver_off_other_tankers(vals['driver_id'])
        return res

    @api.model
    def _water_team_domain(self, user):
        """The drivers a supervisor answers for: up to three levels below him
        in hr.employee.parent_id. Same reach as the record rules in
        security.xml, which spell it out for the same reason (no child_of)."""
        return ['|', '|', ('parent_id.user_id', '=', user.id),
                ('parent_id.parent_id.user_id', '=', user.id),
                ('parent_id.parent_id.parent_id.user_id', '=', user.id)]

    def _water_check_team(self, vals):
        """From Water Delivery > Tankers a supervisor edits only the tankers
        in his tanker groups, and moves only his own team: the tanker must be
        free or driven by one of his drivers, and the driver he puts on it must
        be one of his. Administrators are exempt."""
        user = self.env.user
        if self.env.su or user.has_group('base.group_system'):
            return
        allowed = self._my_tanker_group_vehicle_ids()
        if allowed is not None:
            foreign = self.filtered(lambda v: v.id not in allowed)
            if foreign:
                raise UserError(_(
                    '%s is not in your tanker groups. Ask the Employees '
                    'administrator to add it to one of your groups.',
                    ', '.join(foreign.mapped('name'))))
        if not ({'driver_id', 'x_driver_client_ids'} & set(vals)):
            return
        Employee = self.env['hr.employee'].sudo()
        team = Employee.search(self._water_team_domain(user))
        outside = self.sudo().driver_id - team
        if vals.get('driver_id') and Employee.browse(vals['driver_id']) not in team:
            outside |= Employee.browse(vals['driver_id'])
        if outside:
            raise UserError(_(
                '%s is not in your team, so you cannot change his tanker or his '
                'clients. Ask his own supervisor.', ', '.join(outside.mapped('name'))))

    @api.model
    def _water_check_driver(self, employee_id):
        """An assignment is only useful if the driver can open the app."""
        employee = self.env['hr.employee'].sudo().browse(employee_id)
        user = employee.user_id
        if not user or not user.has_group(DRIVER_GROUP):
            raise UserError(_(
                '%s has no Water Delivery driver login, so he could not issue '
                'notes on this tanker. Ask the administrator to link his user '
                'and give it the Driver role first.', employee.name))

    def _water_move_driver_off_other_tankers(self, employee_id):
        """Assigning a driver here MOVES him: one tanker per driver, or the
        app refuses to guess (`stock.picking._water_assigned_vehicle`). Only
        from this screen -- in Workshop, "default driver" is just information
        and HR may legitimately put one name on several vehicles."""
        others = self.sudo().search([
            ('driver_id', '=', employee_id), ('state', '=', 'confirmed'),
            ('id', 'not in', self.ids),
        ])
        if others:
            others.with_context(ksw_water_tanker_assign=False).write({'driver_id': False})
