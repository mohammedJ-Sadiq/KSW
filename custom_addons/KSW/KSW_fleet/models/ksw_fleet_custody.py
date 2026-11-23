from odoo import _, api, fields, models
from odoo.exceptions import UserError


class KswFleetVehicleCustody(models.Model):
    """Who was personally responsible for a vehicle (custody, عهدة), and when.

    Distinct from the vehicle's Driver: that one is operational, moved day to
    day by dispatch in Water Delivery (a driver lent for a day does not take
    custody). Custody changes only through an approved handover (HR-12) or a
    vehicle-with-driver transfer, so each line names the document behind it.
    """
    _name = 'ksw.fleet.vehicle.custody'
    _description = 'Vehicle Custody History'
    _order = 'date_from desc, id desc'

    vehicle_id = fields.Many2one('ksw.fleet.vehicle', string='Vehicle', required=True, index=True,
                                 ondelete='cascade')
    employee_id = fields.Many2one('hr.employee', string='Custodian', required=True, index=True,
                                  ondelete='restrict')
    date_from = fields.Date(string='From', required=True, default=fields.Date.context_today)
    date_to = fields.Date(string='To', help='Empty while the custody is current.')
    is_current = fields.Boolean(compute='_compute_is_current', search='_search_is_current')
    source = fields.Char(string='Document', help='The handover form or transfer behind this custody.')
    note = fields.Char()

    @api.depends('date_to')
    def _compute_is_current(self):
        for line in self:
            line.is_current = not line.date_to

    def _search_is_current(self, operator, value):
        if operator in ('in', 'not in'):
            wanted = (operator == 'in') == any(value)
        elif operator in ('=', '!='):
            wanted = (operator == '=') == bool(value)
        else:
            return NotImplemented
        return [('date_to', '=' if wanted else '!=', False)]

    def write(self, vals):
        if not self.env.su and not self.env.user.has_group('base.group_system'):
            raise UserError(_('Custody history is written by the handover process, not by hand.'))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise UserError(_('Custody history is never deleted.'))
        return super().unlink()


class KswFleetVehicle(models.Model):
    _inherit = 'ksw.fleet.vehicle'

    # The person answerable for the vehicle (HR-12). Set only through
    # _ksw_set_custodian; write() refuses it from anywhere else.
    x_custodian_id = fields.Many2one('hr.employee', string='Custodian', readonly=True, index=True,
                                     copy=False, help='The employee personally responsible for this '
                                     'vehicle (عهدة). Changes only through an approved handover form.')
    x_custody_ids = fields.One2many('ksw.fleet.vehicle.custody', 'vehicle_id', string='Custody History')

    def write(self, vals):
        if 'x_custodian_id' in vals and not self.env.context.get('ksw_custody_change') and not self.env.su:
            raise UserError(_('The custodian changes only through an approved vehicle handover form (HR-12).'))
        return super().write(vals)

    def _ksw_is_driven(self):
        """A vehicle someone drives. A Tedder is pulled by its head: whoever
        drives the head answers for it, so it is no one's second vehicle."""
        self.ensure_one()
        return self.vehicle_type != 'tank'

    def _ksw_other_driven(self, employee):
        """The driven vehicles this employee already answers for, besides these."""
        if not employee:
            return self.browse()
        return self.sudo().search([('x_custodian_id', '=', employee.id), ('vehicle_type', '!=', 'tank'),
                                   ('id', 'not in', self.ids)])

    def _ksw_custody_full(self, employee):
        """The driven vehicles that leave him no room for these, or nothing."""
        employee = employee.sudo()
        other = self._ksw_other_driven(employee)
        return other if len(other) >= max(employee.x_vehicle_limit, 1) else other.browse()

    def _ksw_check_custodian(self, employee):
        """The last line, whatever route the handover took: one driven vehicle
        per person, and never an archived one. The forms check this (and more)
        before anyone acts; this only stops what slipped past them."""
        if not employee or self.env.context.get('ksw_custody_unchecked'):
            return
        employee = employee.sudo()
        if not employee.active:
            raise UserError(_('%s is archived: a vehicle is not handed to him.', employee.name))
        for vehicle in self.sudo().filtered(lambda v: v._ksw_is_driven()):
            other = vehicle._ksw_custody_full(employee)
            if other:
                raise UserError(_('%(e)s already answers for %(o)s, as many vehicles as he may hold (%(n)s). '
                                  'Hand one over first.', e=employee.name, o=', '.join(other.mapped('name')),
                                  n=max(employee.x_vehicle_limit, 1)))

    def _ksw_set_custodian(self, employee, source=False, date=False, note=False):
        """Close the current custody and open the new one (employee may be
        empty: the vehicle is in nobody's custody, e.g. at the workshop)."""
        date = date or fields.Date.context_today(self)
        History = self.env['ksw.fleet.vehicle.custody'].sudo()
        self.filtered(lambda v: v.x_custodian_id != employee)._ksw_check_custodian(employee)
        for vehicle in self.sudo():
            if vehicle.x_custodian_id == employee:
                continue
            History.search([('vehicle_id', '=', vehicle.id), ('date_to', '=', False)]).write({'date_to': date})
            if employee:
                History.create({'vehicle_id': vehicle.id, 'employee_id': employee.id, 'date_from': date,
                                'source': source, 'note': note})
            vehicle.with_context(ksw_custody_change=True).write({'x_custodian_id': employee.id})
        return True

    @api.model
    def _ksw_seed_custodians(self):
        """Starting custodians: the BAS driver when KSW_workshop_tanker
        knows him, else the default driver. Only vehicles with none yet."""
        vehicles = self.sudo().search([('x_custodian_id', '=', False)])
        has_bas = 'x_bas_driver_id' in self._fields
        for vehicle in vehicles:
            employee = (vehicle.x_bas_driver_id if has_bas else False) or vehicle.driver_id
            if employee:
                vehicle.with_context(ksw_custody_unchecked=True)._ksw_set_custodian(
                    employee, source=_('Starting custodian'), date=fields.Date.context_today(self))
        return True


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    # One driver, one vehicle, unless HR records why this one keeps more
    # (Nov 2026: the Azmi brothers each run two Isuzus).
    x_vehicle_limit = fields.Integer(string='Vehicles He May Hold', default=1, groups='hr.group_hr_user',
                                     help='Driven vehicles (not Tedders) this employee may have in custody at once.')
    x_vehicle_limit_reason = fields.Char(string='Why More Than One', groups='hr.group_hr_user')

    @api.constrains('x_vehicle_limit', 'x_vehicle_limit_reason')
    def _check_vehicle_limit(self):
        for employee in self:
            if employee.x_vehicle_limit < 1:
                raise UserError(_('An employee may hold at least one vehicle.'))
            if employee.x_vehicle_limit > 1 and not (employee.x_vehicle_limit_reason or '').strip():
                raise UserError(_('Say why %s may hold more than one vehicle.', employee.name))
