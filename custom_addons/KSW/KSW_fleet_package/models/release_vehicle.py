from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import RedirectWarning, UserError
from odoo.fields import Domain


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
    new_driver_id = fields.Many2one('hr.employee', string='New Driver',
                                    domain="[('id', 'in', allowed_new_driver_ids)]")
    allowed_new_driver_ids = fields.Many2many('hr.employee.public', compute='_compute_allowed_new_driver_ids')
    custody_request_id = fields.Many2one('ksw.vehicle.custody.request', string='Driver Change', readonly=True,
                                         copy=False)
    fleet_request_id = fields.Many2one('ksw.fleet.request', string='Vehicle Release', readonly=True, copy=False)
    document_requests = fields.Char(compute='_compute_document_requests', string='Document Requests',
                                    compute_sudo=True)

    @api.depends('vehicle_id', 'request_id')
    def _compute_allowed_new_driver_ids(self):
        Custody = self.env['ksw.vehicle.custody.request']
        for row in self:
            vehicle = row.vehicle_id.sudo()
            allowed = vehicle and Custody.with_context(ksw_release_row=row.id)._ksw_driver_candidates(
                vehicle, vehicle.x_department_id)
            row.allowed_new_driver_ids = self.env['hr.employee.public'].sudo().browse(allowed.ids if allowed else [])

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
                problems = self.env['ksw.vehicle.custody.request'].with_context(ksw_release_row=row.id)\
                    ._ksw_driver_problems(driver, vehicle, vehicle.x_department_id)
                if problems:
                    raise UserError('\n'.join(problems))
                row._check_new_driver_documents()

    def _check_new_driver_documents(self):
        """The register holds what the handover needs (the new driver's and
        the vehicle's): when it does not, the supervisor asks HR from the
        message itself instead of being left with a dead end."""
        self.ensure_one()
        self.env['ksw.vehicle.custody.request']._ksw_check_documents(
            self.new_driver_id.sudo(), self.vehicle_id.sudo(), 'KSW_fleet_package.action_request_driver_licence',
            {'active_model': self._name, 'active_id': self.id, 'active_ids': [self.id]},
            can_request=self.request_id.state == 'draft')

    @api.depends('new_driver_id', 'disposition')
    def _compute_document_requests(self):
        Request = self.env['ksw.document.request'].sudo()
        for row in self:
            requests = Request
            if row.disposition == 'driver_change' and row.new_driver_id:
                requests = Request.search([('state', '!=', 'cancelled'), '|',
                                           ('employee_id', '=', row.new_driver_id.id),
                                           ('vehicle_id', '=', row.vehicle_id.id)], order='id desc')
            states = dict(Request._fields['state']._description_selection(self.env))
            row.document_requests = ', '.join('%s: %s' % (r.name, states[r.state]) for r in requests) or False

    def action_request_documents(self):
        """Document requests to HR, the supervisor's to follow: they wait on
        the documents officers' Home and close as HR registers each."""
        self.ensure_one()
        request = self.request_id
        request._check_scope()
        request._check_lines_editable()
        driver = self.new_driver_id.sudo()
        if not driver:
            raise UserError(_('Choose the new driver of %s first.', self.vehicle_id.sudo().name))
        Custody = self.env['ksw.vehicle.custody.request']
        requests = Custody._ksw_request_documents(
            driver, self.vehicle_id.sudo(), _('New driver of %(v)s', v=self.vehicle_id.sudo().name), request.name)
        if not requests:
            raise UserError(_('The register already holds what this handover needs.'))
        return Custody._ksw_documents_requested_note(requests)

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


class KswVehicleCustodyRequest(models.Model):
    _inherit = 'ksw.vehicle.custody.request'

    @api.model
    def _ksw_driver_reservations(self, employees, vehicle, form=None):
        """A release's vehicle plan speaks for its new drivers too, and an
        employee being released takes no vehicle. Within the release being
        planned that holds already in draft; elsewhere once it is submitted."""
        found = super()._ksw_driver_reservations(employees, vehicle, form=form)
        Row = self.env['ksw.employee.release.vehicle'].sudo()
        row = Row.browse(self.env.context.get('ksw_release_row') or [])
        same_request = [('request_id', '=', row.request_id.id)] if row else [('id', '=', False)]
        scope = Domain.OR([same_request, [('request_id.state', '=', 'submitted')]])
        for line in self.env['ksw.employee.release.line'].sudo().search(
                Domain([('employee_id', 'in', employees.ids), ('is_closed', '=', False)]) & scope):
            found.setdefault(line.employee_id.id, _('%(e)s is being released himself (%(r)s).',
                                                    e=line.employee_id.name, r=line.request_id.name))
        planned = Row.search(Domain([
            ('new_driver_id', 'in', employees.ids), ('disposition', '=', 'driver_change'),
            ('custody_request_id', '=', False), ('vehicle_id', '!=', vehicle.id), ('id', '!=', row.id)]) & scope)
        for other in planned:
            found.setdefault(other.new_driver_id.id, _('%(e)s is already the new driver of %(v)s (%(r)s): one '
                                                       'driver takes one vehicle.', e=other.new_driver_id.name,
                                                       v=other.vehicle_id.name, r=other.request_id.name))
        return found
