from datetime import timedelta

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.fields import Domain
from odoo.exceptions import RedirectWarning, UserError, ValidationError

from odoo.addons.KSW_documents.models.ksw_document_import import LICENCE_FOR

# Employee details the form records. Copied onto the form when the employee is
# chosen, never computed: the paper is a record of that day, and a later
# change of phone or iqama must not rewrite a signed form.
_EMPLOYEE_SNAPSHOT = ('name', 'emp_no', 'iqama', 'phone')

KINDS = [
    # Flow 1: the vehicle leaves its department; the custodian hands it in
    # and the workshop receives it. Part of a release request (one GM decision).
    ('hand_in', 'Hand In (Release)'),
    # Flow 2: a department receives a vehicle and names its new custodian.
    ('receive', 'Receive (New Driver)'),
    # Flow 3: one vehicle, two drivers, same department (vacation, suspension…).
    ('driver_change', 'Driver Change'),
]

DOCUMENT_TYPES = [
    ('traffic_license', 'Traffic License'),
    ('insurance', 'Insurance Letter'),
    ('inspection_card', 'Periodic Inspection'),
    ('operation_card', 'Operation Card'),
    ('driver_card', 'Driver Card'),
    ('other', 'Other'),
]

HR_GROUP = 'KSW_vehicle_custody.group_custody_hr'
# A form still on its way: its new user is spoken for, its vehicle is busy.
OPEN_STATES = ('draft', 'hr', 'workshop')
WORKSHOP_GROUP = 'KSW_workshop.group_workshop_technician'


class KswVehicleCustodyRequest(models.Model):
    """Form HR-12, «نموذج تسليم واستلام المركبة»: executing a decision.

    It never carries the decision itself (the GM decides a vehicle request,
    a supervisor decides a driver change in his department). What each part
    checks sets the order:
    Hand In:        opened when the GM sends the vehicle to the workshop; the
                    workshop inspects it while HR collects its documents (in
                    parallel); done when both are.
    Receive:        opened when the vehicle goes to a department that asked;
                    the department names the driver → HR checks his licence
                    (before the handover, the paper's own rule) → the workshop
                    hands it over, which is the arrival.
    Driver Change:  the department's own decision. Done at the supervisor's
                    confirmation, HR told; or, where the department's setting
                    says so (Isuzu branches), like a receipt: supervisor → HR
                    (licence) → workshop checkup → done.
    Done notifies Accounting (financial matters, depreciation) and the
    department; then the paper is printed, signed and filed back as a scan.
    """
    _name = 'ksw.vehicle.custody.request'
    _description = 'Vehicle Handover Form (HR-12)'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(default='New', copy=False, readonly=True)
    kind = fields.Selection(KINDS, string='Request Type', required=True, readonly=True, tracking=True)
    state = fields.Selection([
        ('draft', 'Supervisor'),
        ('hr', 'HR Documents'),
        ('workshop', 'Workshop and HR'),
        ('done', 'Approved'),
        ('signed', 'Signed & Filed'),
        ('refused', 'Refused'),
        ('cancelled', 'Cancelled'),
    ], default='draft', required=True, tracking=True, copy=False, index=True)
    # Hand-in and driver change: the workshop and HR work in parallel; the
    # form is done when both are.
    hr_done = fields.Boolean(string='Documents Checked', readonly=True, copy=False)
    # A driver change that goes through HR and a workshop checkup, not done at
    # the supervisor's confirmation. Read from the department's setting while
    # the form is with the supervisor, then kept: a later change of the
    # setting does not reroute a form on its way.
    through_workshop = fields.Boolean(string='Through Workshop and HR', readonly=True, copy=False)
    workshop_done = fields.Boolean(string='Workshop Done', readonly=True, copy=False)
    form_date = fields.Date(string='Date', required=True, default=fields.Date.context_today)

    vehicle_id = fields.Many2one('ksw.fleet.vehicle', string='Vehicle', required=True, index=True,
                                 ondelete='restrict',
                                 domain="[('x_is_own_fleet', '=', True), ('x_department_id', 'in', allowed_department_ids)]")
    plate_number = fields.Char(related='vehicle_id.plate_number', string='Plate Number')
    vehicle_type = fields.Selection(related='vehicle_id.vehicle_type', string='Vehicle Type')
    department_id = fields.Many2one('hr.department', string='Requesting Department', index=True,
                                    domain="[('id', 'in', allowed_department_ids)]")
    allowed_department_ids = fields.Many2many('hr.department', compute='_compute_allowed_department_ids')
    # The workshop that handles this form: workshop staff see and act on
    # their own workshop's forms only, as with workshop requests. Set when the
    # form is opened (its checkup's workshop, or the receiving department's
    # home workshop); a plain field, so it does not move afterwards.
    workshop_id = fields.Many2one('ksw.workshop', string='Workshop', index=True, readonly=True)
    workshop_request_id = fields.Many2one('ksw.workshop.request', string='Workshop Request',
                                          readonly=True, index=True, ondelete='set null', copy=False)
    origin = fields.Char(readonly=True, help='The release request or transfer this form belongs to.')
    fleet_request_id = fields.Many2one('ksw.fleet.request', string='Release Request', readonly=True,
                                       index=True, ondelete='set null', copy=False)
    move_id = fields.Many2one('ksw.fleet.move', string='Transfer', readonly=True, index=True,
                              ondelete='set null', copy=False)

    # Previous vehicle user / بيانات المستخدم القديم
    previous_employee_id = fields.Many2one('hr.employee', string='Previous User')
    previous_name = fields.Char(string='Previous User Name')
    previous_emp_no = fields.Char(string='Previous User Emp. No')
    previous_iqama = fields.Char(string='Previous User ID #')
    previous_phone = fields.Char(string='Previous User Phone')
    # New vehicle user / بيانات المستخدم الجديد
    new_employee_id = fields.Many2one('hr.employee', string='New User',
                                      domain="[('id', 'in', allowed_new_employee_ids)]")
    # The public model, same ids: a supervisor reads no hr.employee rows.
    allowed_new_employee_ids = fields.Many2many('hr.employee.public', compute='_compute_allowed_new_employee_ids')
    new_name = fields.Char(string='New User Name')
    new_emp_no = fields.Char(string='New User Emp. No')
    new_iqama = fields.Char(string='New User ID #')
    new_phone = fields.Char(string='New User Phone')

    change_reason = fields.Selection([
        ('vacation', 'Vacation'),
        ('suspension', 'Suspension'),
        ('other', 'Other'),
    ], string='Reason for User Change')
    change_reason_note = fields.Char(string='Reason Details')
    workshop_reason = fields.Text(string='Reason to Hand Over to the Workshop')
    supervisor_id = fields.Many2one('hr.employee', string='Supervisor')

    # Administrative use only / خاص بقسم الشؤون الإدارية (HR)
    document_ids = fields.One2many('ksw.vehicle.custody.document', 'request_id', string='Documents')
    new_position = fields.Char(string='Position')
    license_type = fields.Char(string='License Type')
    license_expiry = fields.Date(string='License Expiry')
    admin_remarks = fields.Text(string='Administrative Remarks')
    hr_checked_by_id = fields.Many2one('res.users', string='HR Checked By', readonly=True, copy=False)
    hr_checked_date = fields.Datetime(string='HR Checked On', readonly=True, copy=False)

    # Financial matters / متعلقات مالية
    financial_note_1 = fields.Char(string='Financial Matter 1')
    financial_note_2 = fields.Char(string='Financial Matter 2')
    financial_note_3 = fields.Char(string='Financial Matter 3')
    official_remarks = fields.Text(string='Executive Director Remarks')

    # Delegation process / إجراءات التفويض
    delegation_add_datetime = fields.Datetime(string='New User Added On')
    delegation_remove_datetime = fields.Datetime(string='Old User Removed On')
    delegation_remarks = fields.Text(string='Delegation Remarks')

    # Vehicle inspection form / نموذج فحص المركبة
    inspection_line_ids = fields.One2many('ksw.vehicle.custody.inspection', 'request_id', string='Inspection')
    inspection_datetime = fields.Datetime(string='Inspected On')
    inspection_total = fields.Float(string='Total Value', compute='_compute_inspection_total', store=True)
    # The reading the vehicle was handed over on: the last form of this
    # vehicle the workshop inspected. Each line keeps that reading as a plain
    # copy (the earlier form may be reopened, this one must not move), so the
    # change is what happened since, under the user handing it over now.
    baseline_request_id = fields.Many2one('ksw.vehicle.custody.request', string='Last Inspection',
                                          readonly=True, copy=False, ondelete='set null')
    baseline_datetime = fields.Datetime(related='baseline_request_id.inspection_datetime',
                                        string='Last Inspected On')
    # Maintenance while the user handing the vehicle over held it (display only).
    history_start = fields.Date(string='Custodian Since', compute='_compute_history', compute_sudo=True)
    history_html = fields.Html(string='Maintenance History', compute='_compute_history', compute_sudo=True,
                               sanitize=False)
    receiver_name = fields.Char(string='Vehicle Receiver')
    deliverer_name = fields.Char(string='Vehicle Handed Over By')
    workshop_official_name = fields.Char(string='Workshop Official')
    workshop_done_by_id = fields.Many2one('res.users', string='Workshop Done By', readonly=True, copy=False)

    refuse_reason = fields.Text(readonly=True, copy=False)
    signed_attachment_ids = fields.Many2many(
        'ir.attachment', 'ksw_vehicle_custody_signed_rel', 'request_id', 'attachment_id',
        string='Signed Copy', copy=False, help='The printed form, signed on paper and scanned back.')
    signed_date = fields.Datetime(readonly=True, copy=False)
    signed_by_id = fields.Many2one('res.users', string='Filed By', readonly=True, copy=False)

    # Home ▸ Waiting for you, and the "Waiting for Me" filter.
    x_is_pending_my_action = fields.Boolean(compute='_compute_is_pending_my_action',
                                            search='_search_is_pending_my_action', string='Waiting for Me')
    x_can_supervisor = fields.Boolean(compute='_compute_permissions')
    x_can_hr = fields.Boolean(compute='_compute_permissions')
    x_can_workshop = fields.Boolean(compute='_compute_permissions')

    @api.depends('inspection_line_ids.value')
    def _compute_inspection_total(self):
        for form in self:
            form.inspection_total = sum(form.inspection_line_ids.mapped('value'))

    @api.depends('vehicle_id', 'department_id', 'kind', 'previous_employee_id')
    def _compute_allowed_new_employee_ids(self):
        """Who may be offered as the new user: only people who could take it."""
        for form in self:
            vehicle = form.vehicle_id.sudo()
            department = form._new_user_department()
            if not (vehicle and department):
                form.allowed_new_employee_ids = False
                continue
            allowed = self._ksw_driver_candidates(vehicle, department, form=form) - form.previous_employee_id.sudo()
            form.allowed_new_employee_ids = self.env['hr.employee.public'].sudo().browse(allowed.ids)

    def _new_user_department(self):
        """A receipt's driver is the receiving department's; any other the vehicle's."""
        self.ensure_one()
        return self.department_id if self.kind == 'receive' else self.vehicle_id.sudo().x_department_id

    # ------------------------------------------------------------------
    # Who may take a vehicle: one place, read by the picker, by Submit, by
    # the release's vehicle plan, and (in part) by the custody move itself.
    # ------------------------------------------------------------------
    @api.model
    def _ksw_driver_candidates(self, vehicle, department, form=None):
        Employee = self.env['hr.employee'].sudo()
        people = Employee.search([('department_id', 'child_of', department.id)])
        if vehicle._ksw_is_driven():
            held = self.env['ksw.fleet.vehicle'].sudo().search([
                ('x_custodian_id', 'in', people.ids), ('vehicle_type', '!=', 'tank'), ('id', '!=', vehicle.id)])
            counts = {}
            for other in held:
                counts[other.x_custodian_id.id] = counts.get(other.x_custodian_id.id, 0) + 1
            people = people.filtered(lambda e: counts.get(e.id, 0) < max(e.x_vehicle_limit, 1))
        reserved = self._ksw_driver_reservations(people, vehicle, form=form)
        return people.filtered(lambda e: e.id not in reserved)

    @api.model
    def _ksw_driver_reservations(self, employees, vehicle, form=None):
        """{employee id: where he is already spoken for}. Extended by the
        bridges that plan handovers of their own (a release's vehicles)."""
        found = {}
        # A driver change still in draft is an intention (it is done at
        # submit); a receipt in draft comes from a GM decision and binds.
        for other in self.sudo().search([('new_employee_id', 'in', employees.ids), ('kind', '!=', 'hand_in'),
                                         ('id', '!=', form.id if form else 0), '|',
                                         ('state', 'in', ('hr', 'workshop')),
                                         '&', ('state', '=', 'draft'), ('kind', '=', 'receive')]):
            found.setdefault(other.new_employee_id.id, _('%(e)s is already the new user of %(v)s on %(f)s.',
                                                         e=other.new_employee_id.name, v=other.vehicle_id.name,
                                                         f=other.name))
        return found

    @api.model
    def _ksw_driver_problems(self, employee, vehicle, department, form=None):
        """Why this employee cannot take this vehicle ([] when he can). The
        licence is checked apart: it is the one HR can fix."""
        employee, vehicle = employee.sudo(), vehicle.sudo()
        problems = []
        if not employee.active:
            problems.append(_('%s is archived.', employee.name))
        if not vehicle._ksw_is_driven():
            problems.append(_('%s is a Tedder: whoever drives its head answers for it.', vehicle.name))
        other = vehicle._ksw_custody_full(employee) if vehicle._ksw_is_driven() else vehicle.browse()
        if other:
            problems.append(_('%(e)s already answers for %(o)s, as many vehicles as he may hold (%(n)s).',
                              e=employee.name, o=', '.join(other.mapped('name')),
                              n=max(employee.x_vehicle_limit, 1)))
        if department and employee.department_id not in self.env['hr.department'].sudo().search(
                [('id', 'child_of', department.id)]):
            problems.append(_('%(e)s is not in %(d)s.', e=employee.name, d=department.name))
        reserved = self._ksw_driver_reservations(employee, vehicle, form=form).get(employee.id)
        if reserved:
            problems.append(reserved)
        return problems

    @api.model
    def _ksw_licence_problem(self, employee, vehicle):
        """A licence of the wrong class: HR cannot fix it by registering it."""
        employee, vehicle = employee.sudo(), vehicle.sudo()
        licence = self._ksw_register_licence(employee)
        allowed = LICENCE_FOR.get(vehicle.vehicle_type)
        known = {c for classes in LICENCE_FOR.values() for c in classes} | {'خاصة'}
        if licence and allowed and licence.license_class in known and licence.license_class not in allowed:
            return _('%(e)s holds a %(c)s licence; %(v)s needs %(n)s.', e=employee.name, c=licence.license_class,
                     v=vehicle.name, n=_(' or ').join(allowed))
        return False

    @api.model
    def _ksw_document_gaps(self, employee, vehicle):
        """[(owner, type, 'missing' | 'expired')]: what the register lacks for
        this handover. The vehicle's papers, and the new driver's as the
        custodian he is about to be; only the types HR checks at a handover."""
        gaps = []
        vehicle = vehicle.sudo()
        for doc_type, status in vehicle._ksw_handover_gaps().get(vehicle.id, []):
            gaps.append((vehicle, doc_type, status))
        if employee:
            employee = employee.sudo().with_context(ksw_as_custodian=True)
            for doc_type, status in employee._ksw_handover_gaps().get(employee.id, []):
                gaps.append((employee, doc_type, status))
        return gaps

    @api.model
    def _ksw_gap_label(self, owner, doc_type, status):
        return _('%(o)s: %(t)s (%(s)s)', o=owner.name, t=doc_type.name,
                 s=_('missing') if status == 'missing' else _('expired'))

    @api.model
    def _ksw_request_documents(self, employee, vehicle, reason, origin):
        """Document requests to HR for every gap (reusing the open ones)."""
        Request = self.env['ksw.document.request']
        requests = Request
        for owner, doc_type, _status in self._ksw_document_gaps(employee, vehicle):
            request = Request._ksw_open_for(owner, doc_type)
            if not request:
                request = Request.create({'type_id': doc_type.id, owner._name == 'ksw.fleet.vehicle' and
                                          'vehicle_id' or 'employee_id': owner.id,
                                          'reason': reason, 'origin': origin})
            requests |= request
        return requests

    @api.model
    def _ksw_documents_requested_note(self, requests):
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'type': 'success', 'title': _('Requested from HR'),
            'message': _('%s: HR registers them. Follow them in My Document Requests; you are told as each is '
                         'registered, then submit again.', ', '.join(requests.mapped('name')))}}

    @api.model
    def _ksw_check_documents(self, employee, vehicle, action_xmlid, context, can_request):
        """Refuse the handover while the register lacks a checked document;
        when the caller may ask HR, the message carries the button."""
        wrong_class = self._ksw_licence_problem(employee, vehicle) if employee else False
        if wrong_class:
            raise UserError(wrong_class)
        gaps = self._ksw_document_gaps(employee, vehicle)
        if not gaps:
            return
        Request = self.env['ksw.document.request']
        lines, unrequested = [], False
        for owner, doc_type, status in gaps:
            open_request = Request._ksw_open_for(owner, doc_type)
            label = self._ksw_gap_label(owner, doc_type, status)
            if open_request:
                label += ' ' + _('[HR has %(r)s since %(d)s]', r=open_request.name,
                                 d=fields.Date.to_date(open_request.create_date))
            else:
                unrequested = True
            lines.append('• ' + label)
        message = _('%s cannot change hands until the documents register holds:', vehicle.sudo().name) \
            + '\n' + '\n'.join(lines)
        if unrequested and can_request:
            raise RedirectWarning(message, action_xmlid, _('Request Them from HR'), context)
        raise UserError(message)

    def _check_new_driver(self):
        """Submit and HR: the new user may take the vehicle, and the register
        holds what the handover needs (his and the vehicle's)."""
        for form in self.filtered(lambda f: f.kind != 'hand_in'):
            employee, vehicle = form.new_employee_id.sudo(), form.vehicle_id.sudo()
            problems = self._ksw_driver_problems(employee, vehicle, form._new_user_department(), form=form)
            if problems:
                raise UserError('\n'.join(problems))
            self._ksw_check_documents(
                employee, vehicle, 'KSW_vehicle_custody.action_request_new_user_licence',
                {'active_model': form._name, 'active_id': form.id, 'active_ids': [form.id]},
                can_request=form.state == 'draft')

    def action_cancel_draft(self):
        """The supervisor drops a driver change he has not confirmed."""
        for form in self:
            if form.kind != 'driver_change' or not form.x_can_supervisor:
                raise UserError(_('Only a driver change still with its supervisor is cancelled here.'))
        self._ksw_cancel(_('Cancelled by %s before it was confirmed.', self.env.user.name))
        return True

    def action_request_documents(self):
        self.ensure_one()
        if not self.x_can_supervisor:
            raise UserError(_('%s is not waiting for the supervisor.', self.name))
        requests = self._ksw_request_documents(
            self.new_employee_id.sudo(), self.vehicle_id.sudo(),
            _('Handover of %(v)s', v=self.vehicle_id.sudo().name), self.name)
        if not requests:
            raise UserError(_('The register already holds what this handover needs.'))
        return self._ksw_documents_requested_note(requests)

    @api.depends_context('uid')
    def _compute_allowed_department_ids(self):
        Department = self.env['hr.department']
        domain = [('x_fleet_holder', '=', True)]
        if not self._is_hr() and not self.env['ksw.fleet.request']._ksw_fleet_is_gm():
            domain.append(('id', 'in', self.env.user._ksw_fleet_department_ids()))
        allowed = Department.search(domain)
        for form in self:
            form.allowed_department_ids = allowed

    def _waits_for_hr(self):
        self.ensure_one()
        return self.state == 'hr' or (self.state == 'workshop' and self.kind != 'receive' and not self.hr_done)

    @api.depends_context('uid')
    @api.depends('state', 'department_id', 'hr_done', 'workshop_done')
    def _compute_permissions(self):
        is_hr = self._is_hr()
        is_workshop = self.env.su or self.env.user.has_group(WORKSHOP_GROUP)
        workshops = set(self.env.user._ksw_workshop_scope_ids()) if not self.env.su else None
        scope = set(self.env.user._ksw_fleet_department_ids())
        for form in self:
            form.x_can_supervisor = form.state == 'draft' and (form.department_id.id in scope or is_hr)
            form.x_can_hr = is_hr and form._waits_for_hr()
            form.x_can_workshop = is_workshop and form.state == 'workshop' and not form.workshop_done \
                and (workshops is None or form.workshop_id.id in workshops)

    @api.model
    def _pending_domain(self):
        """Forms waiting for the current user, whatever his role."""
        user = self.env.user
        scope = user._ksw_fleet_department_ids()
        mine = [['&', ('state', '=', 'draft'), '|',
                 '&', ('kind', '=', 'receive'), ('department_id', 'in', scope),
                 '&', ('kind', '=', 'driver_change'), ('create_uid', '=', user.id)],
                # The signed paper is filed by the department that raised it.
                ['&', ('state', '=', 'done'), ('department_id', 'in', scope)]]
        if self._is_hr():
            mine.append(['|', ('state', '=', 'hr'),
                         '&', '&', ('state', '=', 'workshop'), ('kind', '!=', 'receive'), ('hr_done', '=', False)])
        if user.has_group(WORKSHOP_GROUP):
            mine.append(['&', '&', ('state', '=', 'workshop'), ('workshop_done', '=', False),
                         ('workshop_id', 'in', user._ksw_workshop_scope_ids())])
        return Domain.OR(mine)

    @api.depends_context('uid')
    def _compute_is_pending_my_action(self):
        mine = set(self.search(self._pending_domain()).ids)
        for form in self:
            form.x_is_pending_my_action = form.id in mine

    def _search_is_pending_my_action(self, operator, value):
        if operator in ('in', 'not in'):
            wanted = (operator == 'in') == any(value)
        elif operator in ('=', '!='):
            wanted = (operator == '=') == bool(value)
        else:
            return NotImplemented
        domain = self._pending_domain()
        return domain if wanted else ~domain

    def action_open_fleet_request(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'ksw.fleet.request',
                'res_id': self.fleet_request_id.id, 'view_mode': 'form'}

    @api.model
    def _is_hr(self):
        return self.env.su or self.env.user.has_group(HR_GROUP)

    # ------------------------------------------------------------------
    # Employee snapshot
    # ------------------------------------------------------------------
    @api.model
    def _employee_snapshot(self, employee, prefix):
        employee = employee.sudo()
        if not employee:
            return {f'{prefix}_{key}': False for key in _EMPLOYEE_SNAPSHOT}
        # x_employee_no comes from KSW_payroll; this module does not depend on it.
        emp_no = employee['x_employee_no'] if 'x_employee_no' in employee._fields else False
        return {
            f'{prefix}_name': employee.name,
            f'{prefix}_emp_no': emp_no or employee.barcode or False,
            f'{prefix}_iqama': employee.identification_id or False,
            f'{prefix}_phone': employee.mobile_phone or employee.work_phone or False,
        }

    @api.onchange('previous_employee_id')
    def _onchange_previous_employee_id(self):
        self.update(self._employee_snapshot(self.previous_employee_id, 'previous'))

    @api.onchange('new_employee_id')
    def _onchange_new_employee_id(self):
        self.update(self._employee_snapshot(self.new_employee_id, 'new'))
        employee = self.new_employee_id.sudo()
        if employee and not self.new_position:
            self.new_position = employee.job_title or employee.job_id.name

    @api.onchange('vehicle_id')
    def _onchange_vehicle_id(self):
        if self.vehicle_id and self.kind == 'driver_change':
            self.previous_employee_id = self.vehicle_id.sudo().x_custodian_id
            self.department_id = self.vehicle_id.sudo().x_department_id

    def _fill_snapshots(self, vals):
        for prefix in ('previous', 'new'):
            field = f'{prefix}_employee_id'
            if field in vals and f'{prefix}_name' not in vals:
                vals.update(self._employee_snapshot(self.env['hr.employee'].browse(vals[field]), prefix))
        return vals

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('ksw.vehicle.custody.request') or 'New'
            if not self.env.su:
                # Only a supervisor's driver change is raised by hand; the
                # other kinds come from the release and transfer flows.
                if vals.get('kind') != 'driver_change':
                    raise UserError(_('Only a driver change is raised by hand. Hand-ins and '
                                      'receipts come from vehicle releases and transfers.'))
                for key in ('state', 'hr_done', 'workshop_done', 'workshop_request_id', 'workshop_id',
                            'through_workshop'):
                    vals.pop(key, None)
            vehicle = self.env['ksw.fleet.vehicle'].sudo().browse(vals.get('vehicle_id'))
            if vals.get('kind') == 'driver_change' and vehicle:
                vals.setdefault('previous_employee_id', vehicle.x_custodian_id.id)
                vals.setdefault('department_id', vehicle.x_department_id.id)
            self._fill_snapshots(vals)
            if not vals.get('document_ids'):
                vals['document_ids'] = [(0, 0, {'sequence': i, 'document_type': key})
                                        for i, (key, _label) in enumerate(DOCUMENT_TYPES)]
        forms = super().create(vals_list)
        forms._check_driver_change_scope()
        forms._stamp_route()
        forms._check_one_open_form()
        for form in forms.filtered(lambda f: not f.inspection_line_ids):
            form._load_inspection_items()
        forms._sync_activities()
        return forms

    def write(self, vals):
        if not self.env.su and self.filtered(lambda f: f.state in ('signed', 'refused', 'cancelled')) \
                and set(vals) - {'message_main_attachment_id'}:
            raise UserError(_('A signed, refused or cancelled handover form cannot be changed.'))
        if not self.env.su and {'state', 'hr_done', 'workshop_done', 'kind', 'workshop_request_id',
                                'workshop_id', 'through_workshop'} & set(vals):
            raise UserError(_('Use the buttons to move a handover form on.'))
        res = super().write(self._fill_snapshots(dict(vals)))
        if 'department_id' in vals or 'vehicle_id' in vals:
            self._check_driver_change_scope()
            self._stamp_route()
        return res

    def _stamp_route(self):
        """A driver change with its supervisor follows its department's
        setting (Vehicle Departments ▸ Driver Change Through Workshop)."""
        for form in self.sudo().filtered(lambda f: f.kind == 'driver_change' and f.state == 'draft'):
            wanted = form.department_id._ksw_driver_change_through_workshop()
            if form.through_workshop != wanted:
                form.through_workshop = wanted

    def unlink(self):
        if not self.env.su:
            raise UserError(_('A handover form is cancelled, never deleted.'))
        return super().unlink()

    def _check_driver_change_scope(self):
        if self.env.su or self._is_hr():
            return
        scope = set(self.env.user._ksw_fleet_department_ids())
        for form in self.filtered(lambda f: f.kind == 'driver_change'):
            vehicle = form.vehicle_id.sudo()
            if vehicle.x_department_id.id not in scope:
                raise UserError(_('%s is not a vehicle of your departments.', vehicle.name))

    def _check_one_open_form(self):
        for form in self.filtered(lambda f: f.kind == 'driver_change' and f.state in OPEN_STATES):
            other = self.sudo().search([('vehicle_id', '=', form.vehicle_id.id), ('state', 'in', OPEN_STATES),
                                        ('id', '!=', form.id)], limit=1)
            if other:
                raise UserError(_('%(v)s already has %(f)s open: finish or cancel it first.',
                                  v=form.vehicle_id.sudo().name, f=other.name))

    def _check_lines_editable(self):
        if not self.env.su and self.filtered(lambda f: f.state in ('signed', 'refused', 'cancelled')):
            raise UserError(_('A signed, refused or cancelled handover form cannot be changed.'))

    def _load_inspection_items(self, source=None):
        """The checklist for this vehicle's type, or a copy of `source`'s
        inspection (the workshop's, when the vehicle comes out of holding)."""
        self.ensure_one()
        if source and source.inspection_line_ids:
            commands = [(5, 0, 0)] + [(0, 0, {
                'sequence': line.sequence, 'item_id': line.item_id.id, 'name': line.name,
                'depreciation': line.depreciation, 'value': line.value, 'remark': line.remark,
            }) for line in source.inspection_line_ids]
        else:
            items = self.env['ksw.vehicle.custody.checklist'].search([
                '|', ('vehicle_type', '=', False), ('vehicle_type', '=', self.vehicle_id.vehicle_type)])
            commands = [(0, 0, {'sequence': item.sequence, 'item_id': item.id, 'name': item.name})
                        for item in items]
        self.sudo().write({'inspection_line_ids': commands})
        self._stamp_inspection_baseline()

    def _ksw_last_inspection(self):
        """The last form of this vehicle the workshop inspected, other than this one."""
        self.ensure_one()
        return self.sudo().search([
            ('vehicle_id', '=', self.vehicle_id.id), ('id', '!=', self.id or 0), ('workshop_done', '=', True),
            ('state', 'in', ('done', 'signed')), ('inspection_line_ids', '!=', False)], order='id desc', limit=1)

    def _stamp_inspection_baseline(self):
        """Copy the last inspection's reading onto each line while the
        workshop has not inspected yet. A line nobody has touched starts at
        that reading, so the workshop only changes what got worse (or was
        repaired), and an unchanged item reads as no change, not as -20%."""
        for form in self.sudo():
            if form.workshop_done or form.state not in OPEN_STATES:
                continue
            last = form._ksw_last_inspection()
            by_item = {l.item_id.id: l.depreciation for l in last.inspection_line_ids if l.item_id}
            by_name = {l.name: l.depreciation for l in last.inspection_line_ids}
            for line in form.inspection_line_ids:
                previous = by_item.get(line.item_id.id) if line.item_id else None
                if previous is None:
                    previous = by_name.get(line.name, 0.0)
                vals = {'previous_depreciation': previous}
                untouched = line.depreciation == line.previous_depreciation and not line.value
                if untouched:
                    vals['depreciation'] = previous
                line.write(vals)
            form.baseline_request_id = last

    @api.depends_context('lang')
    @api.depends('vehicle_id', 'previous_employee_id', 'form_date', 'workshop_request_id', 'state')
    def _compute_history(self):
        """The vehicle's workshop visits from the day the user handing it over
        became its custodian (to the day he stopped, on a done form): repairs
        and the spare parts fitted, without oil and filters (items ticked
        Routine Service). sudo: shown to the department and HR, who do not
        read workshop requests; a failed read would take the form down."""
        Custody = self.env['ksw.fleet.vehicle.custody']
        Request = self.env['ksw.workshop.request']
        today = fields.Date.context_today(self)
        for form in self:
            form.history_start, form.history_html = False, False
            employee, vehicle = form.previous_employee_id, form.vehicle_id
            if not (employee and vehicle):
                continue
            custody = Custody.search([('vehicle_id', '=', vehicle.id), ('employee_id', '=', employee.id),
                                      ('date_from', '<=', form.form_date or today)],
                                     order='date_from desc, id desc', limit=1)
            if not custody:
                form.history_html = Markup('<p class="text-muted">%s</p>') % _(
                    'No custody record for %(e)s on %(v)s.', e=employee.name, v=vehicle.name)
                continue
            start, end = custody.date_from, custody.date_to or today
            form.history_start = start
            since = fields.Datetime.to_datetime(start)
            until = fields.Datetime.to_datetime(end) + timedelta(days=1)
            requests = Request.search([
                ('vehicle_id', '=', vehicle.id), ('state', '!=', 'rejected'),
                ('id', '!=', form.workshop_request_id.id or 0), '|',
                '&', ('entry_datetime', '>=', since), ('entry_datetime', '<', until),
                '&', '&', ('entry_datetime', '=', False), ('create_date', '>=', since), ('create_date', '<', until),
            ])
            requests = requests.sorted(lambda r: r.entry_datetime or r.create_date, reverse=True)
            form.history_html = form._history_table(employee, start, custody.date_to, requests)

    @api.model
    def _history_table(self, employee, start, end, requests):
        rows, routine_only, parts_count, total = [], 0, 0, 0.0
        oil = self.env.ref('KSW_workshop.request_type_oil_filters', raise_if_not_found=False)
        for request in requests:
            routine = request.part_line_ids.filtered(lambda l: l.part_id.x_routine_service)
            parts = request.part_line_ids - routine
            # An oil / filter service: tagged only Oil & Filters (most of the
            # history, typed as "Change oil" with no itemised parts), or every
            # itemised part on it is a routine item.
            only_oil = oil and request.request_type_ids and request.request_type_ids <= oil
            if not parts and (only_oil or (routine and not request.parts_cost)):
                routine_only += 1
                continue
            cost = request.total_cost - sum(routine.mapped('subtotal'))
            parts_count += len(parts)
            total += cost
            fitted = ' · '.join('%s × %g' % (l.description or l.part_id.display_name or '', l.quantity)
                                for l in parts) or (request.repairs_parts or '')
            when = request.entry_datetime or request.create_date
            rows.append(Markup(
                '<tr><td>%(d)s</td><td><a href="/odoo/ksw.workshop.request/%(id)s">%(n)s</a></td>'
                '<td>%(w)s</td><td>%(p)s</td><td class="text-end">%(c)s</td></tr>') % {
                'd': fields.Date.to_string(when.date()), 'id': request.id, 'n': request.name,
                'w': request.x_work_summary or request.description or '', 'p': fitted,
                'c': '{:,.2f}'.format(cost) if cost else ''})
        period = _('from %(s)s to %(e)s', s=start, e=end) if end else _('since %s', start)
        head = Markup('<p>%s</p>') % (Markup(_('<b>%(e)s</b>, %(p)s: %(v)s, %(n)s, %(t)s SAR.')) % {
            'e': employee.name, 'p': period,
            'v': _('%s workshop visits', len(rows)), 'n': _('%s itemised spare parts', parts_count),
            't': '{:,.2f}'.format(total)})
        if routine_only:
            head += Markup('<p class="text-muted">%s</p>') % _(
                'Not listed: %s oil / filter services.', routine_only)
        if not rows:
            return head
        return head + Markup(
            '<table class="table table-sm o_ksw_custody_history"><thead><tr><th>%(d)s</th><th>%(r)s</th>'
            '<th>%(w)s</th><th>%(p)s</th><th class="text-end">%(c)s</th></tr></thead><tbody>%(rows)s'
            '</tbody></table>') % {
            'd': _('Date'), 'r': _('Request'), 'w': _('Work Done'), 'p': _('Spare Parts'), 'c': _('Cost'),
            'rows': Markup('').join(rows)}

    # ------------------------------------------------------------------
    # The route
    # ------------------------------------------------------------------
    def _advance(self, state, **vals):
        self.sudo().write(dict(vals, state=state))
        if state == 'workshop':
            # The reading the workshop starts from is the latest one there is.
            self._stamp_inspection_baseline()
        self._sync_activities()

    def action_submit(self):
        """Supervisor. A receipt goes to HR, then the workshop hands it over.
        A driver change is the department's own decision between two of its
        drivers: once every check passes it is done, and HR is told. Where
        the department's setting says so it goes the receipt's way instead."""
        for form in self:
            if not form.x_can_supervisor:
                raise UserError(_('%s is not waiting for the supervisor.', form.name))
            if not form.new_employee_id:
                raise UserError(_('Choose the new user of the vehicle.'))
            if form.kind == 'driver_change':
                if not form.change_reason:
                    raise UserError(_('Give the reason for the user change.'))
                if form.new_employee_id == form.previous_employee_id:
                    raise UserError(_('The new user is the current custodian.'))
                if form.vehicle_id.sudo().x_is_released:
                    raise UserError(_('%s is on a release request; its department is giving it up.',
                                      form.vehicle_id.name))
                if form.vehicle_id.sudo().x_allocation_state != 'assigned':
                    raise UserError(_('%s is being moved; a driver change waits until it is back '
                                      'in its department.', form.vehicle_id.name))
        self._stamp_route()
        self._check_one_open_form()
        self._check_new_driver()
        for form in self:
            if form.kind == 'driver_change' and not form.through_workshop:
                form._fill_licence_from_register()
                form._advance('done')
                form._apply_custody()
                form._send_notifications()
            else:
                form._advance('hr')
        return True

    def _fill_licence_from_register(self):
        """The new user's licence as the register holds it, where the form
        has none yet: HR checks it, it does not type it again."""
        for form in self.sudo().filtered(lambda f: f.new_employee_id and not f.license_expiry):
            doc = self._ksw_register_licence(form.new_employee_id)
            if doc:
                form.write({'license_type': doc.license_class or form.license_type,
                            'license_expiry': doc.expiry_date})

    @api.model
    def _ksw_register_licence(self, employee):
        """The employee's confirmed driving licence in the documents register."""
        return self.env['ksw.document'].sudo().search([
            ('employee_id', '=', employee.id), ('type_id.code', '=', 'driving_licence'),
            ('state', '=', 'confirmed')], limit=1)

    def _check_new_user_licence(self):
        self._fill_licence_from_register()
        for form in self.filtered('new_employee_id'):
            if not (form.license_type and form.license_expiry):
                raise UserError(_('Record the new user\'s licence type and expiry: the vehicle is '
                                  'not handed over without a valid licence.'))
            if form.license_expiry < fields.Date.context_today(self):
                raise UserError(_('The new user\'s licence expired on %s.', form.license_expiry))

    def action_hr_check(self):
        """HR's section: the vehicle's documents, and for a new user his
        licence. HR completes a section; the decision was the GM's."""
        for form in self:
            if not form.x_can_hr:
                raise UserError(_('Only HR completes the documents of %s at this step.', form.name))
        self._check_new_driver()
        self._check_new_user_licence()
        stamp = {'hr_checked_by_id': self.env.user.id, 'hr_checked_date': fields.Datetime.now()}
        for form in self.sudo():
            if form.state == 'hr':
                # HR's part is over: on a driver change nothing waits for it.
                form._advance('workshop', hr_done=True, **stamp)
                form._ensure_checkup()
            else:
                form.write(dict(stamp, hr_done=True))
                form._finish_if_complete()
        return True

    def action_workshop_done(self):
        """The workshop: inspected the vehicle (and, on a receipt, handed it
        to the new driver, which is the arrival)."""
        for form in self:
            if not form.x_can_workshop:
                raise UserError(_('Only the workshop completes %s at this step.', form.name))
            if not form.inspection_datetime:
                raise ValidationError(_('Fill in when the workshop inspected the vehicle.'))
        for form in self.sudo():
            form.write({'workshop_done': True, 'workshop_done_by_id': self.env.user.id,
                        'workshop_official_name': form.workshop_official_name or self.env.user.name})
            form._finish_if_complete()
        return True

    def _finish_if_complete(self):
        """Done when every part is: a receipt after HR (already done before
        the workshop step), the others when HR and the workshop both are."""
        for form in self.sudo():
            if form.state != 'workshop' or not form.workshop_done:
                self._sync_activities()
                continue
            if form.kind != 'receive' and not form.hr_done:
                form._sync_activities()
                continue
            form._advance('done')
            form._apply_custody()
            form._send_notifications()

    def action_open_refuse(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Refuse %s', self.name),
            'res_model': 'ksw.vehicle.custody.refuse.wizard', 'view_mode': 'form', 'target': 'new',
            'context': {'default_request_id': self.id},
        }

    def _refuse(self, reason):
        """A supervisor's driver change can be refused by HR or the workshop
        (licence, vehicle state). A hand-in or receipt executes the GM's
        decision: it is changed on the vehicle request, not refused here."""
        for form in self:
            if form.kind != 'driver_change':
                raise UserError(_('A hand-in or receipt is part of a GM decision: the GM '
                                  'changes it on the vehicle request, not here.'))
            if not (form.x_can_hr or form.x_can_workshop):
                raise UserError(_('%s cannot be refused at this step.', form.name))
        self._advance('refused', refuse_reason=reason)
        for form in self:
            form.sudo().message_post(body=Markup(_('Refused: %s')) % reason,
                                     partner_ids=form.create_uid.partner_id.ids,
                                     subtype_xmlid='mail.mt_comment')
            checkup = form.sudo().workshop_request_id
            if checkup and checkup.state not in ('completed', 'rejected'):
                checkup.message_post(body=Markup(_('Driver change %(f)s was refused: %(r)s')) % {
                    'f': form.name, 'r': reason}, subtype_xmlid='mail.mt_note')

    def _ensure_checkup(self):
        """A driver change through the workshop: raise its checkup at the
        workshop of the vehicle's department once HR has checked the driver.
        The checkup closes only with this form signed (KSW_workshop bridge)."""
        for form in self.sudo().filtered(lambda f: f.kind == 'driver_change' and not f.workshop_request_id):
            vehicle = form.vehicle_id
            requester = form.create_uid.employee_id or form.supervisor_id or form.previous_employee_id
            checkup = self.env['ksw.workshop.request'].sudo().create({
                'employee_id': requester.id,
                'client_id': vehicle.client_id.id,
                'vehicle_type': vehicle.vehicle_type,
                'vehicle_id': vehicle.id,
                'driver_id': (form.previous_employee_id or requester).id,
                'workshop_id': self.env['ksw.workshop']._for_department(form.department_id).id,
                'description': _('Checkup for driver change (%(f)s): %(a)s → %(b)s',
                                 f=form.name, a=form.previous_name or '-', b=form.new_name or '-'),
            })
            form.write({'workshop_request_id': checkup.id, 'workshop_id': checkup.workshop_id.id})
            form._sync_activities()

    def _apply_custody(self):
        """What a done form changes. A hand-in leaves the vehicle in nobody's
        custody (it is at the workshop); a receipt or a driver change gives it
        to the new user, who also becomes its operating driver. A receipt is
        the arrival: the vehicle now belongs to the department."""
        self.ensure_one()
        vehicle = self.vehicle_id.sudo()
        if self.kind == 'hand_in':
            vehicle._ksw_set_custodian(self.env['hr.employee'], source=self.name)
        else:
            vehicle._ksw_set_custodian(self.new_employee_id, source=self.name)
            vehicle.write({'driver_id': self.new_employee_id.id})
        if self.kind == 'receive' and self.move_id.state == 'in_transfer':
            self.move_id.sudo()._ksw_fleet_complete({})

    def _send_notifications(self):
        """Who needs to know once it is done: Accounting when a driver hands
        a vehicle in (financial matters, the depreciation value, his account
        statement), and the department, which also files the signed paper.
        HR is not copied on a hand-in or receipt: it was a step. A driver
        change done at confirmation had no HR step, so HR is told, with what
        the register lacks; one through the workshop is treated as a receipt."""
        accounting = self.env.ref('KSW_vehicle_custody.group_custody_accounting').sudo().all_user_ids
        accounting = accounting.filtered(lambda u: u.active and not u.share)
        technical = self.env.ref('base.user_admin') | self.env.ref('base.user_root')
        hr_users = self.env.ref(HR_GROUP).sudo().all_user_ids.filtered(
            lambda u: u.active and not u.share) - technical
        for form in self.sudo():
            partners = form.department_id._ksw_fleet_users().partner_id
            if form.kind in ('hand_in', 'driver_change'):
                partners |= accounting.partner_id
            told_hr = form.kind == 'driver_change' and not form.hr_done
            if told_hr:
                partners |= hr_users.partner_id
            partners -= self.env.user.partner_id
            body = Markup(_('<b>%(n)s done</b>: %(k)s, %(v)s. %(p)s → %(w)s. Depreciation value: %(t)s.')) % {
                'n': form.name, 'k': dict(form._fields['kind']._description_selection(self.env))[form.kind],
                'v': form.vehicle_id.name, 'p': form.previous_name or '-', 'w': form.new_name or '-',
                't': '{:,.2f}'.format(form.inspection_total)}
            if told_hr:
                lacking = form.document_ids.filtered(lambda d: d.register_status in ('missing', 'expired'))
                if lacking:
                    labels = dict(lacking._fields['document_type']._description_selection(self.env))
                    status = dict(lacking._fields['register_status']._description_selection(self.env))
                    body += Markup('<br/><b>%s</b> ') % _('For HR, missing or expired in the register:')
                    body += Markup(', ').join(Markup('%(t)s (%(s)s)') % {
                        't': labels[d.document_type], 's': status[d.register_status]} for d in lacking)
            form.message_post(body=body, partner_ids=partners.ids, subtype_xmlid='mail.mt_comment')

    def action_mark_signed(self):
        """File the signed paper back: the scan is required."""
        for form in self:
            if form.state != 'done':
                raise UserError(_('%s is not approved yet; the paper is signed after approval.', form.name))
            if not form.signed_attachment_ids:
                raise UserError(_('Attach the signed and scanned form first (Signed Copy).'))
        self.sudo().write({'state': 'signed', 'signed_date': fields.Datetime.now(),
                           'signed_by_id': self.env.user.id})
        for form in self:
            form.signed_attachment_ids.sudo().write({'res_model': form._name, 'res_id': form.id})
            form.sudo().message_post(body=Markup(_('Signed copy filed by %s.')) % self.env.user.name,
                                     subtype_xmlid='mail.mt_note')
        self._sync_activities()
        return True

    def action_print(self):
        return self.env.ref('KSW_vehicle_custody.action_report_vehicle_custody').report_action(self)

    def action_open_workshop_request(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'ksw.workshop.request',
                'res_id': self.workshop_request_id.id, 'view_mode': 'form'}

    def _ksw_cancel(self, reason):
        for form in self.sudo().filtered(lambda f: f.state not in ('signed', 'refused', 'cancelled')):
            form.state = 'cancelled'
            form.message_post(body=reason, subtype_xmlid='mail.mt_note')
        self._sync_activities()

    # ------------------------------------------------------------------
    # To-dos: whoever the form waits for
    # ------------------------------------------------------------------
    def _sync_activities(self):
        todo = self.env.ref('KSW_vehicle_custody.mail_activity_custody')
        # admin and OdooBot hold these roles for system access, not as people
        # who do the work (same rule as KSW_workshop's notifications).
        technical = self.env.ref('base.user_admin') | self.env.ref('base.user_root')
        hr_users = self.env.ref(HR_GROUP).sudo().all_user_ids.filtered(
            lambda u: u.active and not u.share) - technical
        for form in self.sudo().exists():
            wanted = {}
            if form.state == 'draft':
                users = form.department_id._ksw_fleet_users() if form.kind == 'receive' else form.create_uid
                for user in users:
                    if form.kind == 'receive':
                        wanted[user.id] = _('Name the new driver: %s', form.vehicle_id.name)
                    elif form.through_workshop:
                        wanted[user.id] = _('Submit to HR: %s', form.vehicle_id.name)
                    else:
                        wanted[user.id] = _('Confirm the driver change: %s', form.vehicle_id.name)
            if form._waits_for_hr():
                for user in hr_users:
                    wanted[user.id] = _('HR documents: %s', form.vehicle_id.name)
            if form.state == 'workshop' and not form.workshop_done:
                managers = self.env.ref('KSW_workshop.group_workshop_manager').sudo().all_user_ids
                workshop = form.workshop_id
                for user in (managers - technical).filtered(lambda u: u.active and not u.share and (
                        not workshop or workshop.id in u._ksw_workshop_scope_ids())):
                    wanted[user.id] = _('Workshop: %s', form.vehicle_id.name)
            elif form.state == 'done':
                for user in form.department_id._ksw_fleet_users() | form.create_uid:
                    wanted[user.id] = _('File the signed HR-12: %s', form.name)
            mine = form.activity_ids.filtered(lambda a: a.activity_type_id == todo)
            for activity in mine:
                if wanted.get(activity.user_id.id) == activity.summary:
                    wanted.pop(activity.user_id.id)
                else:
                    activity.unlink()
            for user_id, summary in wanted.items():
                form.activity_schedule(activity_type_id=todo.id, user_id=user_id, summary=summary)


# HR-12's document lines and the register's types: the line shows what the
# register holds instead of HR re-checking it at every handover. The driver
# card belongs to the user who keeps (or receives) the vehicle.
_REGISTER_CODES = {
    'traffic_license': 'istimara', 'insurance': 'insurance', 'inspection_card': 'mvpi',
    'operation_card': 'operation_card', 'driver_card': 'driver_card',
}


class KswVehicleCustodyDocument(models.Model):
    _name = 'ksw.vehicle.custody.document'
    _description = 'Vehicle Handover Form: Document'
    _order = 'sequence, id'

    request_id = fields.Many2one('ksw.vehicle.custody.request', required=True, index=True, ondelete='cascade')
    sequence = fields.Integer(default=10)
    document_type = fields.Selection(DOCUMENT_TYPES, string='Type', required=True)
    present = fields.Boolean(string='Handed Over')
    remark = fields.Char()
    register_status = fields.Selection([
        ('valid', 'Valid'), ('expiring', 'Expiring Soon'), ('expired', 'Expired'),
        ('missing', 'Not in the Register'), ('none', ''),
    ], string='In the Register', compute='_compute_register', compute_sudo=True)
    register_expiry = fields.Date(string='Expires', compute='_compute_register', compute_sudo=True)

    @api.depends('document_type', 'request_id.vehicle_id', 'request_id.new_employee_id',
                 'request_id.previous_employee_id')
    def _compute_register(self):
        Document = self.env['ksw.document']
        for line in self:
            code = _REGISTER_CODES.get(line.document_type)
            form = line.request_id
            if not code:
                line.register_status, line.register_expiry = 'none', False
                continue
            if code == 'driver_card':
                domain = [('employee_id', '=', (form.new_employee_id or form.previous_employee_id).id)]
            else:
                domain = [('vehicle_id', '=', form.vehicle_id.id)]
            doc = Document.search(domain + [('type_id.code', '=', code), ('state', '=', 'confirmed')], limit=1)
            if not doc:
                line.register_status, line.register_expiry = 'missing', False
            else:
                line.register_status = 'valid' if doc.expiry_status == 'none' else doc.expiry_status
                line.register_expiry = doc.expiry_date

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        lines.request_id._check_lines_editable()
        return lines

    def write(self, vals):
        self.request_id._check_lines_editable()
        return super().write(vals)

    def unlink(self):
        self.request_id._check_lines_editable()
        return super().unlink()


class KswVehicleCustodyInspection(models.Model):
    _name = 'ksw.vehicle.custody.inspection'
    _description = 'Vehicle Handover Form: Inspection Item'
    _order = 'sequence, id'

    request_id = fields.Many2one('ksw.vehicle.custody.request', required=True, index=True, ondelete='cascade')
    sequence = fields.Integer(default=10)
    item_id = fields.Many2one('ksw.vehicle.custody.checklist', string='Checklist Item', ondelete='set null')
    name = fields.Char(string='Item', required=True)
    previous_depreciation = fields.Float(string='At Last Inspection %', readonly=True,
                                         help='The reading of the last inspection, which the vehicle was '
                                              'handed over on.')
    depreciation = fields.Float(string='Depreciation %')
    depreciation_change = fields.Float(string='Change Since %', compute='_compute_depreciation_change',
                                       help='Depreciation since the last inspection: what happened while '
                                            'the user handing it over now held it.')
    value = fields.Float(string='Value')
    remark = fields.Char()

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        lines.request_id._check_lines_editable()
        return lines

    def write(self, vals):
        self.request_id._check_lines_editable()
        return super().write(vals)

    def unlink(self):
        self.request_id._check_lines_editable()
        return super().unlink()

    @api.depends('depreciation', 'previous_depreciation')
    def _compute_depreciation_change(self):
        for line in self:
            line.depreciation_change = line.depreciation - line.previous_depreciation

    @api.constrains('depreciation')
    def _check_depreciation(self):
        for line in self:
            if not 0 <= line.depreciation <= 100:
                raise ValidationError(_('Depreciation is a percentage between 0 and 100.'))


class KswVehicleCustodyChecklist(models.Model):
    """The inspection checklist, per vehicle type. Configuration, not code:
    the paper's list is tanker-shaped; an Isuzu or a trailer head needs its own."""
    _name = 'ksw.vehicle.custody.checklist'
    _description = 'Vehicle Inspection Checklist Item'
    _order = 'sequence, id'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    vehicle_type = fields.Selection([
        ('trailer', 'Trailer (تريلا)'),
        ('isuzu', 'Isuzu'),
        ('tank', 'Tedder (تيدر)'),
        ('other', 'Other'),
    ], string='Vehicle Type', help='Empty: checked on every vehicle type.')
    active = fields.Boolean(default=True)
