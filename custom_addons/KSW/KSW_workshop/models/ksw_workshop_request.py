from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class KswWorkshopRequest(models.Model):
    _name = 'ksw.workshop.request'
    _description = 'Workshop Service Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'

    # Fields whose write access is restricted beyond what the ACL alone
    # expresses. See write() below — kept as a single source of truth so
    # both the guard and any future audit of "who can touch what" read
    # from the same place.
    _IMMUTABLE_FIELDS = {'employee_id'}
    _STATE_FIELDS = {'state', 'rejection_reason', 'completion_date'}
    _INTERNAL_NOTE_FIELDS = {'note', 'note_to_requester'}
    _REPORT_FIELDS = {
        'entry_datetime', 'exit_datetime', 'odometer_reading', 'tire_pressure',
        'tire_bolts', 'work_statement', 'repairs_parts', 'technician_id',
        'parts_cost', 'labor_cost',
    }
    _REQUESTER_FIELDS = {'vehicle_id', 'driver_id', 'description'}

    name = fields.Char(default='New', copy=False, readonly=True, tracking=True)

    employee_id = fields.Many2one(
        'hr.employee', string='Requested By', required=True,
        default=lambda self: self.env.user.employee_id, readonly=True,
    )
    department_id = fields.Many2one(related='employee_id.department_id', store=True, readonly=True)
    work_email = fields.Char(related='employee_id.work_email', readonly=True)
    mobile_phone = fields.Char(related='employee_id.mobile_phone', readonly=True)
    job_title = fields.Char(related='employee_id.job_title', readonly=True)

    vehicle_id = fields.Many2one('ksw.fleet.vehicle', string='Vehicle', required=True, tracking=True)
    driver_id = fields.Many2one('hr.employee', string='Driver', tracking=True)

    description = fields.Text(required=True, tracking=True)

    state = fields.Selection([
        ('new', 'New'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('rejected', 'Rejected'),
    ], default='new', tracking=True, copy=False, required=True)

    note = fields.Text(string='Internal Note')
    note_to_requester = fields.Text(string='Note to Requester')
    rejection_reason = fields.Text()
    completion_date = fields.Datetime(readonly=True, copy=False)
    duration_days = fields.Integer(compute='_compute_duration_days')

    # --- Repair report (workshop technician / manager only) ---
    entry_datetime = fields.Datetime(string='Entry Date/Time')
    exit_datetime = fields.Datetime(string='Exit Date/Time')
    odometer_reading = fields.Integer(string='Odometer Reading')
    tire_pressure = fields.Char(string='Tire Pressure')
    tire_bolts = fields.Char(string='Tire Bolts')
    work_statement = fields.Text(string='Statement of Required Work')
    repairs_parts = fields.Text(string='Repairs & Spare Parts')
    technician_id = fields.Many2one('hr.employee', string='Technician')
    parts_cost = fields.Float(string='Spare Parts Cost')
    labor_cost = fields.Float(string='Labor Cost')

    # --- History import bookkeeping ---
    x_legacy_uid = fields.Char(string='Legacy UID', readonly=True, copy=False)
    x_imported = fields.Boolean(default=False, readonly=True, copy=False)

    @api.depends('completion_date', 'create_date')
    def _compute_duration_days(self):
        for request in self:
            if request.completion_date and request.create_date:
                request.duration_days = (request.completion_date - request.create_date).days
            else:
                request.duration_days = 0

    @api.onchange('vehicle_id')
    def _onchange_vehicle_id(self):
        for request in self:
            if request.vehicle_id.driver_id:
                request.driver_id = request.vehicle_id.driver_id

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        is_manager = self.env.su or self.env.user.has_group('KSW_workshop.group_workshop_manager')
        for vals in vals_list:
            if not is_manager:
                vals['employee_id'] = self.env.user.employee_id.id
            if not vals.get('name') or vals['name'] == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('ksw.workshop.request') or 'New'
        requests = super().create(vals_list)
        for request in requests:
            request._notify_workshop_managers()
        return requests

    def write(self, vals):
        if not self.env.su:
            user = self.env.user
            is_manager = user.has_group('KSW_workshop.group_workshop_manager')
            is_technician = user.has_group('KSW_workshop.group_workshop_technician')
            touched = set(vals.keys())

            if touched & self._IMMUTABLE_FIELDS:
                raise UserError(_('The requester of a workshop request cannot be changed.'))

            if touched & (self._STATE_FIELDS | self._INTERNAL_NOTE_FIELDS) and not is_manager:
                raise UserError(_('Only the workshop manager can do this.'))

            if touched & self._REPORT_FIELDS:
                if not (is_manager or is_technician):
                    raise UserError(_('Only the workshop manager or a technician can edit the repair report.'))
                for request in self:
                    if request.state != 'in_progress':
                        raise UserError(_('The repair report can only be edited while the request is In Progress.'))

            if touched & self._REQUESTER_FIELDS and not is_manager:
                for request in self:
                    if request.employee_id.user_id != user or request.state != 'new':
                        raise UserError(_('You can only edit your own request while it is New.'))

        return super().write(vals)

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def _check_manager(self):
        if not self.env.su and not self.env.user.has_group('KSW_workshop.group_workshop_manager'):
            raise UserError(_('Only the workshop manager can do this.'))

    def action_start(self):
        self._check_manager()
        for request in self:
            if request.state != 'new':
                raise UserError(_('Only new requests can be started.'))
        self.write({'state': 'in_progress'})

    def action_reject(self):
        self._check_manager()
        for request in self:
            if request.state != 'new':
                raise UserError(_('Only new requests can be rejected.'))
            if not request.rejection_reason:
                raise UserError(_('Please provide a rejection reason before rejecting the request.'))
        self.write({'state': 'rejected'})
        for request in self:
            request._notify_requester(_('Your workshop request was rejected.'))

    def action_complete(self):
        self._check_manager()
        for request in self:
            if request.state != 'in_progress':
                raise UserError(_('Only in-progress requests can be completed.'))
        self.write({'state': 'completed', 'completion_date': fields.Datetime.now()})
        for request in self:
            request._notify_requester(_('Your workshop request has been completed.'))

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------
    def _notify_workshop_managers(self):
        group = self.env.ref('KSW_workshop.group_workshop_manager', raise_if_not_found=False)
        partner_ids = group.user_ids.mapped('partner_id').ids if group else []
        if not partner_ids:
            return
        body = Markup(
            '<b>New workshop request %(name)s</b><br/>'
            '<b>Requested by:</b> %(requester)s<br/>'
            '<b>Vehicle:</b> %(vehicle)s<br/>'
            '<b>Description:</b> %(description)s'
        ) % {
            'name': self.name,
            'requester': self.employee_id.name,
            'vehicle': self.vehicle_id.display_name,
            'description': self.description,
        }
        self.sudo().message_post(body=body, partner_ids=partner_ids, subtype_xmlid='mail.mt_comment')

    def _notify_requester(self, instruction):
        self.ensure_one()
        partner = self.employee_id.user_id.partner_id
        if not partner:
            return
        body = Markup('<b>%(instruction)s</b>') % {'instruction': instruction}
        if self.note_to_requester:
            body += Markup('<br/>%(note)s') % {'note': self.note_to_requester}
        self.sudo().message_post(body=body, partner_ids=[partner.id], subtype_xmlid='mail.mt_comment')
