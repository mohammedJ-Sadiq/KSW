from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import RedirectWarning, UserError


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
    licence_request = fields.Char(compute='_compute_licence_request', string='Licence Request', compute_sudo=True)
    new_driver_holds = fields.Char(compute='_compute_new_driver_holds', string='New Driver Already Holds',
                                   compute_sudo=True)

    @api.depends('new_driver_id', 'disposition')
    def _compute_new_driver_holds(self):
        Vehicle = self.env['ksw.fleet.vehicle']
        for row in self:
            held = Vehicle.search([('x_custodian_id', '=', row.new_driver_id.id),
                                   ('id', '!=', row.vehicle_id.id)]) \
                if row.disposition == 'driver_change' and row.new_driver_id else Vehicle
            row.new_driver_holds = ', '.join(held.mapped('name'))

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
                raise UserError(_('%(e)s answers for %(v)s: say in Vehicles in Their Custody what happens to '
                                  'it if he leaves.',
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
                leaving = self.env['ksw.employee.release.line'].sudo().search([
                    ('employee_id', '=', driver.id), ('is_closed', '=', False), '|',
                    ('request_id', '=', row.request_id.id), ('request_id.state', '=', 'submitted')], limit=1)
                if leaving:
                    raise UserError(_('%(d)s, the new driver of %(v)s, is being released himself (%(r)s).',
                                      d=driver.name, v=vehicle.name, r=leaving.request_id.name))
                twice = self.sudo().search([
                    ('id', '!=', row.id), ('disposition', '=', 'driver_change'), ('new_driver_id', '=', driver.id),
                    ('custody_request_id', '=', False), '|',
                    ('request_id', '=', row.request_id.id), ('request_id.state', '=', 'submitted')], limit=1)
                if twice:
                    raise UserError(_('%(d)s is chosen as the new driver of both %(v)s and %(o)s (%(r)s): '
                                      'one driver takes one of them.', d=driver.name, v=vehicle.name,
                                      o=twice.vehicle_id.name, r=twice.request_id.name))
                row._check_new_driver_licence()

    def _check_new_driver_licence(self):
        """Licences are HR's register: when it lacks his, the supervisor asks
        HR from the message itself instead of being left with a dead end."""
        self.ensure_one()
        driver, vehicle = self.new_driver_id.sudo(), self.vehicle_id.sudo()
        licence = self.env['ksw.vehicle.custody.request']._ksw_register_licence(driver)
        if not licence:
            message = _('%(d)s, the new driver of %(v)s, has no confirmed driving licence in the documents '
                        'register.', d=driver.name, v=vehicle.name)
        elif licence.expiry_date and licence.expiry_date < fields.Date.context_today(self):
            message = _('The driving licence of %(d)s, the new driver of %(v)s, expired on %(x)s.',
                        d=driver.name, v=vehicle.name, x=licence.expiry_date)
        else:
            return
        pending = self._licence_request(driver)
        if pending:
            raise UserError(message + ' ' + _('HR has the request %(r)s since %(d)s.', r=pending.name,
                                              d=fields.Date.to_date(pending.create_date)))
        if self.request_id.state != 'draft':
            raise UserError(message)
        raise RedirectWarning(message + ' ' + _('HR registers it.'),
                              'KSW_fleet_package.action_request_driver_licence', _('Request It from HR'),
                              {'active_model': self._name, 'active_id': self.id, 'active_ids': [self.id]})

    def _licence_request(self, driver):
        return self.env['ksw.document.request']._ksw_open_for(
            driver, self.env.ref('KSW_documents.type_driving_licence'))

    @api.depends('new_driver_id', 'disposition')
    def _compute_licence_request(self):
        Request = self.env['ksw.document.request']
        for row in self:
            request = Request
            if row.disposition == 'driver_change' and row.new_driver_id:
                request = Request.sudo().search([
                    ('employee_id', '=', row.new_driver_id.id),
                    ('type_id', '=', self.env.ref('KSW_documents.type_driving_licence').id),
                    ('state', '!=', 'cancelled')], order='id desc', limit=1)
            state = dict(request._fields['state']._description_selection(self.env)).get(request.state)
            row.licence_request = request and '%s: %s' % (request.name, state) or False

    def action_request_licence(self):
        """A document request to HR, the supervisor's to follow: it waits on
        the documents officers' Home and closes when HR confirms the licence."""
        self.ensure_one()
        request = self.request_id
        request._check_scope()
        request._check_lines_editable()
        driver = self.new_driver_id.sudo()
        if not driver:
            raise UserError(_('Choose the new driver of %s first.', self.vehicle_id.sudo().name))
        dr = self._licence_request(driver)
        if not dr:
            dr = self.env['ksw.document.request'].create({
                'type_id': self.env.ref('KSW_documents.type_driving_licence').id, 'employee_id': driver.id,
                'reason': _('New driver of %(v)s', v=self.vehicle_id.sudo().name), 'origin': request.name})
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'type': 'success', 'title': _('Requested from HR'),
            'message': _('%(r)s: HR registers the driving licence of %(d)s. Follow it in Employee Transfers, '
                         'My Document Requests; you are told when it is registered, then submit again.',
                         r=dr.name, d=driver.name)}}

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

    def _ksw_decide(self, decision, addition_line=False, effective_date=False, note=False):
        # The plan may have gone stale since submit (licence, the driver taken).
        if decision in ('transfer', 'terminate'):
            self.sudo().x_vehicle_ids._check_ready()
        return super()._ksw_decide(decision, addition_line=addition_line, effective_date=effective_date,
                                   note=note)

    def _ksw_on_leaving(self):
        res = super()._ksw_on_leaving()
        self.sudo().x_vehicle_ids._launch()
        return res


class KswEmployeeRequest(models.Model):
    _inherit = 'ksw.employee.request'

    x_vehicle_ids = fields.One2many('ksw.employee.release.vehicle', 'request_id',
                                    string='Vehicles in Their Custody')

    def _submit_note_extra(self):
        extra = super()._submit_note_extra()
        rows = self.sudo().x_vehicle_ids
        if not rows:
            return extra
        extra += Markup('<br/><b>%s</b>') % _('Their vehicles, if they leave:')
        for row in rows:
            plan = _('driver change to %s', row.new_driver_id.name) if row.disposition == 'driver_change' \
                else _('released to the workshop')
            extra += Markup('<br/>• %(v)s (%(e)s): %(p)s') % {'v': row.vehicle_id.name, 'e': row.employee_id.name,
                                                             'p': plan}
        return extra


class KswEmployeeReleaseDecisionWizard(models.TransientModel):
    _inherit = 'ksw.employee.release.decision.wizard'

    x_vehicle_ids = fields.Many2many('ksw.employee.release.vehicle', compute='_compute_x_vehicle_ids',
                                     string='Their Vehicles, If They Leave')

    @api.depends('release_line_ids')
    def _compute_x_vehicle_ids(self):
        for wizard in self:
            wizard.x_vehicle_ids = wizard.release_line_ids.sudo().x_vehicle_ids
