from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

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
WORKSHOP_GROUP = 'KSW_workshop.group_workshop_technician'


class KswVehicleCustodyRequest(models.Model):
    """Form HR-12, «نموذج تسليم واستلام المركبة», as a request with its route.

    Hand In:        HR → GM (decided on the release request) → Workshop → done
    Receive:        Supervisor names the driver → HR → Workshop hands over → done
    Driver Change:  Supervisor → HR → Workshop checkup → done (no GM: the
                    vehicle stays in its department, only its custodian changes)

    Done applies the custody and sends HR and Accounting an information copy.
    The paper is then printed, signed by hand and filed back as a scan.
    """
    _name = 'ksw.vehicle.custody.request'
    _description = 'Vehicle Handover Form (HR-12)'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(default='New', copy=False, readonly=True)
    kind = fields.Selection(KINDS, string='Request Type', required=True, readonly=True, tracking=True)
    state = fields.Selection([
        ('draft', 'Supervisor'),
        ('hr', 'HR Review'),
        ('gm', 'GM Decision'),
        ('workshop', 'Workshop'),
        ('done', 'Approved'),
        ('signed', 'Signed & Filed'),
        ('refused', 'Refused'),
        ('cancelled', 'Cancelled'),
    ], default='draft', required=True, tracking=True, copy=False, index=True)
    # A hand-in whose GM decision is already taken (the GM took the vehicle,
    # or decided the release) goes from HR straight to the workshop.
    gm_decided = fields.Boolean(readonly=True, copy=False)
    form_date = fields.Date(string='Date', required=True, default=fields.Date.context_today)

    vehicle_id = fields.Many2one('ksw.fleet.vehicle', string='Vehicle', required=True, index=True,
                                 ondelete='restrict',
                                 domain="[('x_is_own_fleet', '=', True), ('x_department_id', 'in', allowed_department_ids)]")
    plate_number = fields.Char(related='vehicle_id.plate_number', string='Plate Number')
    vehicle_type = fields.Selection(related='vehicle_id.vehicle_type', string='Vehicle Type')
    department_id = fields.Many2one('hr.department', string='Requesting Department', index=True,
                                    domain="[('id', 'in', allowed_department_ids)]")
    allowed_department_ids = fields.Many2many('hr.department', compute='_compute_allowed_department_ids')
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
    new_employee_id = fields.Many2one('hr.employee', string='New User')
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

    x_can_supervisor = fields.Boolean(compute='_compute_permissions')
    x_can_hr = fields.Boolean(compute='_compute_permissions')
    x_can_workshop = fields.Boolean(compute='_compute_permissions')

    @api.depends('inspection_line_ids.value')
    def _compute_inspection_total(self):
        for form in self:
            form.inspection_total = sum(form.inspection_line_ids.mapped('value'))

    @api.depends_context('uid')
    def _compute_allowed_department_ids(self):
        Department = self.env['hr.department']
        domain = [('x_fleet_holder', '=', True)]
        if not self._is_hr() and not self.env['ksw.fleet.request']._ksw_fleet_is_gm():
            domain.append(('id', 'in', self.env.user._ksw_fleet_department_ids()))
        allowed = Department.search(domain)
        for form in self:
            form.allowed_department_ids = allowed

    @api.depends_context('uid')
    @api.depends('state', 'department_id')
    def _compute_permissions(self):
        is_hr = self._is_hr()
        is_workshop = self.env.su or self.env.user.has_group(WORKSHOP_GROUP)
        scope = set(self.env.user._ksw_fleet_department_ids())
        for form in self:
            form.x_can_supervisor = form.state == 'draft' and (form.department_id.id in scope or is_hr)
            form.x_can_hr = form.state == 'hr' and is_hr
            form.x_can_workshop = form.state == 'workshop' and is_workshop

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
                for key in ('state', 'gm_decided', 'workshop_request_id'):
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
        for form in forms.filtered(lambda f: not f.inspection_line_ids):
            form._load_inspection_items()
        forms._sync_activities()
        return forms

    def write(self, vals):
        if not self.env.su and self.filtered(lambda f: f.state in ('signed', 'refused', 'cancelled')) \
                and set(vals) - {'message_main_attachment_id'}:
            raise UserError(_('A signed, refused or cancelled handover form cannot be changed.'))
        if not self.env.su and {'state', 'gm_decided', 'kind', 'workshop_request_id'} & set(vals):
            raise UserError(_('Use the buttons to move a handover form on.'))
        res = super().write(self._fill_snapshots(dict(vals)))
        if 'department_id' in vals or 'vehicle_id' in vals:
            self._check_driver_change_scope()
        return res

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

    # ------------------------------------------------------------------
    # The route
    # ------------------------------------------------------------------
    def _advance(self, state, **vals):
        self.sudo().write(dict(vals, state=state))
        self._sync_activities()

    def action_submit(self):
        """Supervisor → HR."""
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
        self._advance('hr')
        return True

    def action_hr_approve(self):
        """HR → GM (hand-in not yet decided) or → Workshop. The licence rule
        of the paper is enforced here: no vehicle to a driver without one."""
        for form in self:
            if not form.x_can_hr:
                raise UserError(_('Only HR approves %s at this step.', form.name))
            if form.new_employee_id and not (form.license_type and form.license_expiry):
                raise UserError(_('Record the new user\'s licence type and expiry: the vehicle is '
                                  'not handed over without a valid licence.'))
            if form.new_employee_id and form.license_expiry < fields.Date.context_today(self):
                raise UserError(_('The new user\'s licence expired on %s.', form.license_expiry))
        for form in self.sudo():
            stamp = {'hr_checked_by_id': self.env.user.id, 'hr_checked_date': fields.Datetime.now()}
            if form.kind == 'hand_in' and not form.gm_decided:
                form._advance('gm', **stamp)
            else:
                form._advance('workshop', **stamp)
                form._ensure_checkup()
        return True

    def action_open_refuse(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Refuse %s', self.name),
            'res_model': 'ksw.vehicle.custody.refuse.wizard', 'view_mode': 'form', 'target': 'new',
            'context': {'default_request_id': self.id},
        }

    def _refuse(self, reason):
        for form in self:
            if not (form.x_can_hr or (form.kind == 'driver_change' and form.state == 'workshop'
                                      and form.x_can_workshop)):
                raise UserError(_('%s cannot be refused at this step.', form.name))
            if form.kind != 'driver_change':
                raise UserError(_('A hand-in or receipt is part of a GM decision: the GM '
                                  'changes it on the vehicle request, not here.'))
        self._advance('refused', refuse_reason=reason)
        for form in self:
            form.sudo().message_post(body=Markup(_('Refused: %s')) % reason,
                                     partner_ids=form.create_uid.partner_id.ids,
                                     subtype_xmlid='mail.mt_comment')

    def _gm_decided_to_workshop(self, workshop_request=None):
        """Called by the vehicle request when the GM sends the vehicle to the
        workshop: a hand-in past HR moves on, one still with HR goes straight
        to the workshop after HR."""
        for form in self.sudo():
            vals = {'gm_decided': True}
            if workshop_request:
                vals['workshop_request_id'] = workshop_request.id
            form.write(vals)
            if form.state == 'gm':
                form._advance('workshop')
        return True

    def _ensure_checkup(self):
        """A driver change is checked by the workshop: raise its checkup
        request when it reaches the workshop."""
        for form in self.sudo().filtered(lambda f: f.kind == 'driver_change' and not f.workshop_request_id):
            vehicle = form.vehicle_id
            requester = self.env.user.employee_id or form.supervisor_id or form.previous_employee_id
            workshop_request = self.env['ksw.workshop.request'].sudo().create({
                'employee_id': requester.id,
                'client_id': vehicle.client_id.id,
                'vehicle_type': vehicle.vehicle_type,
                'vehicle_id': vehicle.id,
                'driver_id': (form.previous_employee_id or requester).id,
                'workshop_id': self.env['ksw.workshop']._for_department(form.department_id).id,
                'description': _('Checkup for driver change (%(f)s): %(a)s → %(b)s',
                                 f=form.name, a=form.previous_name or '-', b=form.new_name or '-'),
            })
            form.write({'workshop_request_id': workshop_request.id})

    def action_workshop_done(self):
        """Workshop → Approved: the vehicle was inspected (and, on a receipt,
        handed over). The custody changes now; HR and Accounting are told."""
        for form in self:
            if not form.x_can_workshop:
                raise UserError(_('Only the workshop completes %s at this step.', form.name))
            if not form.inspection_datetime:
                raise ValidationError(_('Fill in when the workshop inspected the vehicle.'))
        for form in self:
            form._advance('done', workshop_done_by_id=self.env.user.id,
                          workshop_official_name=form.workshop_official_name or self.env.user.name)
            form._apply_custody()
            form._send_information_copy()
        return True

    def _apply_custody(self):
        """What an approved form changes. A hand-in leaves the vehicle in
        nobody's custody (it is at the workshop); a receipt or a driver change
        gives it to the new user, who also becomes its operating driver."""
        self.ensure_one()
        vehicle = self.vehicle_id.sudo()
        if self.kind == 'hand_in':
            vehicle._ksw_set_custodian(self.env['hr.employee'], source=self.name)
        else:
            vehicle._ksw_set_custodian(self.new_employee_id, source=self.name)
            vehicle.write({'driver_id': self.new_employee_id.id})

    def _send_information_copy(self):
        """The paper's last step: HR (delegation, Absher) and Accounting
        (financial matters, the driver's account statement) get a copy."""
        users = self.env.ref(HR_GROUP).sudo().all_user_ids \
            | self.env.ref('KSW_vehicle_custody.group_custody_accounting').sudo().all_user_ids
        users = users.filtered(lambda u: u.active and not u.share) - self.env.user
        for form in self.sudo():
            body = Markup(_('<b>%(n)s approved</b>: %(k)s, %(v)s. %(p)s → %(w)s. '
                            'For your action if any: delegation (HR), financial matters (Accounting).')) % {
                'n': form.name, 'k': dict(form._fields['kind']._description_selection(self.env))[form.kind],
                'v': form.vehicle_id.name, 'p': form.previous_name or '-', 'w': form.new_name or '-'}
            form.message_post(body=body, partner_ids=users.partner_id.ids, subtype_xmlid='mail.mt_comment')

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
        hr_users = self.env.ref(HR_GROUP).sudo().all_user_ids.filtered(lambda u: u.active and not u.share)
        for form in self.sudo().exists():
            wanted = {}
            if form.state == 'draft':
                users = form.department_id._ksw_fleet_users() if form.kind == 'receive' else form.create_uid
                for user in users:
                    wanted[user.id] = _('Name the new driver: %s', form.vehicle_id.name) \
                        if form.kind == 'receive' else _('Submit to HR: %s', form.vehicle_id.name)
            elif form.state == 'hr':
                for user in hr_users:
                    wanted[user.id] = _('HR review: %s', form.vehicle_id.name)
            elif form.state == 'workshop':
                managers = self.env.ref('KSW_workshop.group_workshop_manager').sudo().all_user_ids
                workshop = form.workshop_request_id.workshop_id
                for user in managers.filtered(lambda u: u.active and not u.share and (
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


class KswVehicleCustodyDocument(models.Model):
    _name = 'ksw.vehicle.custody.document'
    _description = 'Vehicle Handover Form: Document'
    _order = 'sequence, id'

    request_id = fields.Many2one('ksw.vehicle.custody.request', required=True, index=True, ondelete='cascade')
    sequence = fields.Integer(default=10)
    document_type = fields.Selection(DOCUMENT_TYPES, string='Type', required=True)
    present = fields.Boolean(string='Handed Over')
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


class KswVehicleCustodyInspection(models.Model):
    _name = 'ksw.vehicle.custody.inspection'
    _description = 'Vehicle Handover Form: Inspection Item'
    _order = 'sequence, id'

    request_id = fields.Many2one('ksw.vehicle.custody.request', required=True, index=True, ondelete='cascade')
    sequence = fields.Integer(default=10)
    item_id = fields.Many2one('ksw.vehicle.custody.checklist', string='Checklist Item', ondelete='set null')
    name = fields.Char(string='Item', required=True)
    depreciation = fields.Float(string='Depreciation %')
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
