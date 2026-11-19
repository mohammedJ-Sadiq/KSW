from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class KswEmployeeReleaseVehicle(models.Model):
    """A vehicle in a released employee's custody, and what his department
    decided for it if he leaves: another driver in the department (an HR-12
    driver change, no GM: the vehicle stays) or released to the workshop (a
    vehicle release request, which the GM decides in Vehicle Allocation).
    Moving him WITH it is a vehicle release "with its driver" instead."""
    _name = 'ksw.employee.release.vehicle'
    _description = 'Vehicle of a Released Employee'
    _order = 'release_line_id, id'

    release_line_id = fields.Many2one('ksw.employee.release.line', required=True, index=True, ondelete='cascade')
    request_id = fields.Many2one('ksw.employee.request', required=True, index=True, ondelete='cascade')
    employee_id = fields.Many2one(related='release_line_id.employee_id', string='Employee')
    vehicle_id = fields.Many2one('ksw.fleet.vehicle', string='Vehicle', required=True, readonly=True)
    vehicle_department_id = fields.Many2one(related='vehicle_id.x_department_id', string='Vehicle Department')
    disposition = fields.Selection([
        ('driver_change', 'Another Driver in Our Department'),
        ('holding', 'Release to the Workshop'),
    ], string='If He Leaves')
    new_driver_id = fields.Many2one('hr.employee', string='New Driver')
    custody_request_id = fields.Many2one('ksw.vehicle.custody.request', string='Driver Change', readonly=True,
                                         copy=False)
    fleet_request_id = fields.Many2one('ksw.fleet.request', string='Vehicle Release', readonly=True, copy=False)

    def write(self, vals):
        if not self.env.su:
            if set(vals) - {'disposition', 'new_driver_id'}:
                raise UserError(_('Only what happens to the vehicle is chosen here.'))
            self.request_id._check_lines_editable()
        return super().write(vals)

    def _check_ready(self):
        for row in self:
            vehicle = row.vehicle_id.sudo()
            if not row.disposition:
                raise UserError(_('%(e)s answers for %(v)s: say what happens to it if he leaves.',
                                  e=row.employee_id.sudo().name, v=vehicle.name))
            if row.disposition == 'driver_change':
                driver = row.new_driver_id.sudo()
                if not driver:
                    raise UserError(_('Choose the new driver of %s.', vehicle.name))
                if driver == row.employee_id.sudo():
                    raise UserError(_('The new driver of %s is the employee being released.', vehicle.name))
                scope = self.env['hr.department'].sudo().search([('id', 'child_of', vehicle.x_department_id.id)])
                if driver.department_id not in scope:
                    raise UserError(_('%(d)s is not in %(dep)s: a driver change stays in the vehicle\'s '
                                      'department.', d=driver.name, dep=vehicle.x_department_id.name))

    def _launch(self):
        """The GM's decision takes the employee away: carry out what his
        department decided for each vehicle."""
        Custody = self.env['ksw.vehicle.custody.request'].sudo()
        FleetRequest = self.env['ksw.fleet.request'].sudo()
        for row in self.sudo().filtered(lambda r: r.disposition == 'driver_change' and not r.custody_request_id):
            form = Custody.create({
                'kind': 'driver_change', 'vehicle_id': row.vehicle_id.id,
                'new_employee_id': row.new_driver_id.id, 'change_reason': 'other',
                'change_reason_note': _('%(e)s released (%(r)s)', e=row.employee_id.name,
                                        r=row.request_id.name),
                'origin': row.request_id.name,
            })
            form.action_submit()
            row.custody_request_id = form
        holding = self.sudo().filtered(lambda r: r.disposition == 'holding' and not r.fleet_request_id)
        for department in holding.vehicle_id.x_department_id:
            rows = holding.filtered(lambda r: r.vehicle_id.x_department_id == department)
            request = rows[0].request_id
            fleet_request = FleetRequest.create({
                'request_type': 'release', 'department_id': department.id,
                'user_id': request.requested_by_id.id,
                'reason': _('Their driver leaves the department (%s).', request.name),
                'release_line_ids': [(0, 0, {'vehicle_id': r.vehicle_id.id,
                                             'note': _('%s released', r.employee_id.name)}) for r in rows],
            })
            fleet_request.action_submit()
            rows.write({'fleet_request_id': fleet_request.id})
        for request in self.sudo().request_id:
            rows = self.filtered(lambda r: r.request_id == request)
            parts = [_('%(v)s: driver change to %(d)s (%(f)s)', v=r.vehicle_id.name, d=r.new_driver_id.name,
                       f=r.custody_request_id.name) for r in rows if r.custody_request_id] + \
                    [_('%(v)s: released to the workshop (%(f)s)', v=r.vehicle_id.name, f=r.fleet_request_id.name)
                     for r in rows if r.fleet_request_id]
            if parts:
                request.message_post(body=Markup(_('Vehicles of the leaving employees: %s')) % '; '.join(parts),
                                     subtype_xmlid='mail.mt_note')


class KswEmployeeReleaseLine(models.Model):
    _inherit = 'ksw.employee.release.line'

    x_vehicle_ids = fields.One2many('ksw.employee.release.vehicle', 'release_line_id',
                                    string='Vehicles in His Custody')

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        lines._sync_custody_vehicles()
        return lines

    def write(self, vals):
        res = super().write(vals)
        if 'employee_id' in vals:
            self._sync_custody_vehicles()
        return res

    def _sync_custody_vehicles(self):
        """One row per vehicle he answers for, while the release is a draft."""
        Row = self.env['ksw.employee.release.vehicle'].sudo()
        Vehicle = self.env['ksw.fleet.vehicle'].sudo()
        for line in self.sudo().filtered(lambda l: l.request_id.state == 'draft'):
            vehicles = Vehicle.search([('x_custodian_id', '=', line.employee_id.id)]) \
                if line.employee_id else Vehicle
            line.x_vehicle_ids.filtered(lambda r: r.vehicle_id not in vehicles).unlink()
            for vehicle in vehicles - line.x_vehicle_ids.vehicle_id:
                Row.create({'release_line_id': line.id, 'request_id': line.request_id.id,
                            'vehicle_id': vehicle.id})

    def _check_releasable(self):
        super()._check_releasable()
        # Custody may have changed since the line was added.
        self._sync_custody_vehicles()
        self.sudo().x_vehicle_ids._check_ready()

    def _ksw_on_leaving(self):
        res = super()._ksw_on_leaving()
        self.sudo().x_vehicle_ids._launch()
        return res


class KswEmployeeRequest(models.Model):
    _inherit = 'ksw.employee.request'

    x_vehicle_ids = fields.One2many('ksw.employee.release.vehicle', 'request_id',
                                    string='Vehicles in Their Custody')
