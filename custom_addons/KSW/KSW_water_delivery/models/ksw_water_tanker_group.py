from odoo import fields, models


class KswWaterTankerGroup(models.Model):
    """A set of tankers a supervisor answers for (Trailers, Isuzu, Tanks...).

    Water Delivery > Tankers shows a supervisor only the tankers in his
    groups. Kept by the Employees Administrator, not by the supervisors: who
    looks after which vehicles is an HR decision, and a supervisor who could
    widen his own groups would make the whole thing decoration.
    """
    _name = 'ksw.water.tanker.group'
    _description = 'Tanker Group'
    _order = 'sequence, name'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    vehicle_ids = fields.Many2many(
        'ksw.fleet.vehicle', 'ksw_water_tanker_group_vehicle_rel', 'group_id', 'vehicle_id',
        string='Tankers', domain="[('x_is_own_vehicle', '=', True)]")
    supervisor_ids = fields.Many2many(
        'hr.employee', 'ksw_water_tanker_group_employee_rel', 'group_id', 'employee_id',
        string='Supervisors')
    vehicle_count = fields.Integer(compute='_compute_vehicle_count', string='# Tankers')

    _name_uniq = models.Constraint('unique(name)', 'A tanker group with this name already exists.')

    def _compute_vehicle_count(self):
        for group in self:
            group.vehicle_count = len(group.vehicle_ids)
