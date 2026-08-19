from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


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
        'parts_cost', 'parts_extra_cost', 'labor_cost',
    }
    _REQUESTER_FIELDS = {
        'client_id', 'vehicle_type', 'vehicle_id', 'driver_id', 'description', 'request_type',
        'x_cash_customer_name', 'x_cash_vehicle_number', 'x_cash_tank_number',
        'x_cash_plate_number', 'x_cash_driver_name', 'x_cash_brand', 'x_cash_model', 'x_cash_year',
    }
    _MANAGER_ONLY_FIELDS = {'is_cash_customer'}

    name = fields.Char(default='New', copy=False, readonly=True, tracking=True)

    employee_id = fields.Many2one(
        'hr.employee', string='Requested By', required=True,
        default=lambda self: self.env.user.employee_id, readonly=True,
    )
    department_id = fields.Many2one(related='employee_id.department_id', store=True, readonly=True)
    work_email = fields.Char(related='employee_id.work_email', readonly=True)
    mobile_phone = fields.Char(related='employee_id.mobile_phone', readonly=True)
    job_title = fields.Char(related='employee_id.job_title', readonly=True)

    request_type = fields.Selection([
        ('mechanical', 'Mechanical'),
        ('bodywork', 'Bodywork / Welding'),
        ('oil_filters', 'Oil & Filters'),
        ('electrical', 'Electrical'),
    ], string='Request Type', tracking=True)

    is_cash_customer = fields.Boolean(string='Cash Customer', tracking=True, copy=False)
    x_can_toggle_cash_customer = fields.Boolean(
        compute='_compute_x_can_toggle_cash_customer', compute_sudo=True,
    )

    client_id = fields.Many2one(
        'res.partner', string='Client', tracking=True,
        default=lambda self: self.env.company.partner_id,
    )
    vehicle_type = fields.Selection([
        ('trailer', 'Trailer'),
        ('isuzu', 'Isuzu'),
        ('tank', 'Tank'),
        ('other', 'Other'),
    ], string='Vehicle Type', tracking=True)
    # Note: the default-context for this field's quick-create dialog
    # (default_client_id/default_vehicle_type) is set on the view's <field>
    # tag, not here — a field-level context= kwarg is expected to be a
    # static dict, not a dynamic per-record expression like domain= is;
    # passing a dynamic string here reaches web_read() as a raw string and
    # crashes with "with_context() argument after ** must be a mapping,
    # not str" the moment this field is touched during an onchange.
    vehicle_id = fields.Many2one(
        'ksw.fleet.vehicle', string='Vehicle', tracking=True,
        compute='_compute_vehicle_id', store=True, readonly=False,
        domain="[('client_id', '=', client_id), ('vehicle_type', '=', vehicle_type), "
               "'|', ('state', '=', 'confirmed'), ('id', '=', vehicle_id)]",
    )
    driver_id = fields.Many2one('hr.employee', string='Driver', tracking=True)

    # --- Cash Customer (walk-in): free-text mirror of the structured
    # client/vehicle fields above, for one-off work with no client/vehicle
    # record. Same shape as x_legacy_requester_name/email below. ---
    x_cash_customer_name = fields.Char(string='Customer Name (Cash)')
    x_cash_vehicle_number = fields.Char(string='Vehicle Number (Cash)')
    x_cash_tank_number = fields.Char(string='Tank Number (Cash)')
    x_cash_plate_number = fields.Char(string='Plate Number (Cash)')
    x_cash_driver_name = fields.Char(string='Driver Name (Cash)')
    x_cash_brand = fields.Char(string='Brand (Cash)')
    x_cash_model = fields.Char(string='Model (Cash)')
    x_cash_year = fields.Integer(string='Year (Cash)')

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
    part_line_ids = fields.One2many(
        'ksw.workshop.part.line', 'request_id', string='Spare Parts Used', copy=False,
    )
    part_lines_cost = fields.Float(
        string='Catalogued Parts', compute='_compute_part_lines_cost', store=True, readonly=True,
        help='Sum of the Spare Parts Used lines below. Read-only — it mirrors that table.',
    )
    parts_extra_cost = fields.Float(
        string='Other Parts Cost',
        help='Cost of one-off / uncatalogued items described in Repairs & Spare Parts.',
    )
    parts_cost = fields.Float(
        string='Spare Parts Cost', compute='_compute_parts_cost', store=True, readonly=True,
        help='Catalogued Parts + Other Parts Cost.',
    )
    labor_cost = fields.Float(string='Labor Cost')

    # --- History import bookkeeping ---
    x_legacy_uid = fields.Char(string='Legacy UID', readonly=True, copy=False)
    x_imported = fields.Boolean(default=False, readonly=True, copy=False)
    x_legacy_requester_name = fields.Char(
        string='Requester Name (as submitted)', readonly=True, copy=False,
        help="The name typed into the legacy form, kept for reference even when "
             "employee_id had to fall back to a placeholder (no matching Odoo account).")
    x_legacy_requester_email = fields.Char(
        string='Requester Email (as submitted)', readonly=True, copy=False,
        help="The email address the legacy form was submitted from, kept for "
             "reference regardless of whether it matched an Odoo user.")

    @api.depends('part_line_ids.subtotal')
    def _compute_part_lines_cost(self):
        totals = {r.id: 0.0 for r in self}
        for request, total in self.env['ksw.workshop.part.line'].sudo()._read_group(
                [('request_id', 'in', self.ids)], ['request_id'], ['subtotal:sum']):
            totals[request.id] = total
        for request in self:
            request.part_lines_cost = totals.get(request.id, 0.0)

    @api.depends('part_lines_cost', 'parts_extra_cost')
    def _compute_parts_cost(self):
        for request in self:
            request.parts_cost = request.part_lines_cost + request.parts_extra_cost

    @api.depends('completion_date', 'create_date')
    def _compute_duration_days(self):
        for request in self:
            if request.completion_date and request.create_date:
                request.duration_days = (request.completion_date - request.create_date).days
            else:
                request.duration_days = 0

    @api.depends_context('uid')
    def _compute_x_can_toggle_cash_customer(self):
        can_toggle = self.env.su or self.env.user.has_group('KSW_workshop.group_workshop_manager')
        for request in self:
            request.x_can_toggle_cash_customer = can_toggle

    @api.depends('client_id', 'vehicle_type')
    def _compute_vehicle_id(self):
        for request in self:
            if request.vehicle_id and (
                request.vehicle_id.client_id != request.client_id
                or (request.vehicle_type and request.vehicle_id.vehicle_type != request.vehicle_type)
            ):
                request.vehicle_id = False

    @api.onchange('vehicle_id')
    def _onchange_vehicle_id(self):
        for request in self:
            if request.vehicle_id.driver_id:
                request.driver_id = request.vehicle_id.driver_id

    @api.onchange('is_cash_customer')
    def _onchange_is_cash_customer(self):
        for request in self:
            if request.is_cash_customer:
                request.client_id = False
                request.vehicle_type = False
                request.vehicle_id = False
            else:
                request.x_cash_customer_name = False
                request.x_cash_vehicle_number = False
                request.x_cash_tank_number = False
                request.x_cash_plate_number = False
                request.x_cash_driver_name = False
                request.x_cash_brand = False
                request.x_cash_model = False
                request.x_cash_year = False

    @api.constrains('is_cash_customer', 'client_id', 'vehicle_type', 'vehicle_id',
                     'x_cash_customer_name', 'x_cash_vehicle_number')
    def _check_vehicle_and_cash_customer(self):
        for request in self:
            if request.is_cash_customer:
                if not request.x_cash_customer_name or not request.x_cash_vehicle_number:
                    raise ValidationError(_(
                        'Cash Customer requests require at least a customer name and a '
                        'vehicle/tank number.'
                    ))
                continue
            if not request.client_id or not request.vehicle_id:
                raise ValidationError(_(
                    'Client and Vehicle are required unless this is a Cash Customer request.'
                ))
            if request.vehicle_id.client_id != request.client_id:
                raise ValidationError(_(
                    'The selected vehicle does not belong to %s.', request.client_id.display_name
                ))
            if request.vehicle_type and request.vehicle_id.vehicle_type != request.vehicle_type:
                raise ValidationError(_(
                    'The selected vehicle is not a %s.',
                    dict(request._fields['vehicle_type'].selection).get(request.vehicle_type),
                ))

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        is_manager = self.env.su or self.env.user.has_group('KSW_workshop.group_workshop_manager')
        for vals in vals_list:
            if not is_manager:
                vals['employee_id'] = self.env.user.employee_id.id
                vals['is_cash_customer'] = False
            if not vals.get('name') or vals['name'] == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('ksw.workshop.request') or 'New'
        requests = super().create(vals_list)
        for request in requests:
            request._notify_workshop_managers()
        return requests

    def _check_report_edit_rights(self):
        """May the caller edit this request's repair report right now?

        Single source of truth: called both from write() below (for
        _REPORT_FIELDS) and from ksw.workshop.part.line's create/write/unlink
        guard, so the Spare Parts Used table can never drift out of sync
        with the rest of the repair report.
        """
        if self.env.su:
            return
        user = self.env.user
        if not (user.has_group('KSW_workshop.group_workshop_manager')
                or user.has_group('KSW_workshop.group_workshop_technician')):
            raise UserError(_('Only the workshop manager or a technician can edit the repair report.'))
        # sudo() for the state read only: once a request leaves in_progress
        # and this caller isn't its assigned technician, the technician
        # record rule hides the row entirely, and a plain (non-sudo) read
        # of .state would raise Odoo's generic ir.rule "not found" error
        # instead of this method's clean UserError — same message either
        # way the caller reached this check (write() vs. a part line).
        for request in self.sudo():
            if request.state != 'in_progress':
                raise UserError(_('The repair report can only be edited while the request is In Progress.'))

    def write(self, vals):
        if not self.env.su:
            user = self.env.user
            is_manager = user.has_group('KSW_workshop.group_workshop_manager')
            touched = set(vals.keys())

            if touched & self._IMMUTABLE_FIELDS:
                raise UserError(_('The requester of a workshop request cannot be changed.'))

            if touched & (self._STATE_FIELDS | self._INTERNAL_NOTE_FIELDS) and not is_manager:
                raise UserError(_('Only the workshop manager can do this.'))

            if touched & self._MANAGER_ONLY_FIELDS:
                if not is_manager:
                    raise UserError(_('Only the workshop manager can toggle Cash Customer.'))
                for request in self:
                    if request.state != 'new':
                        raise UserError(_('Cash Customer can only be changed while the request is New.'))

            if touched & self._REPORT_FIELDS:
                self._check_report_edit_rights()

            if touched & self._REQUESTER_FIELDS and not is_manager:
                for request in self:
                    if request.employee_id.user_id != user or request.state != 'new':
                        raise UserError(_('You can only edit your own request while it is New.'))

        return super().write(vals)

    def unlink(self):
        # part_line_ids uses ondelete='cascade' — a DB-level cascade that
        # skips ksw.workshop.part.line's Python unlink(), so its movements
        # (and thus qty_on_hand) would go stale. Go through the ORM first;
        # ksw.workshop.part.line.unlink() takes care of its own moves.
        self.mapped('part_line_ids').sudo().unlink()
        return super().unlink()

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
        client_text = self.x_cash_customer_name if self.is_cash_customer else self.client_id.display_name
        vehicle_text = self.x_cash_vehicle_number if self.is_cash_customer else self.vehicle_id.display_name
        body = Markup(
            '<b>New workshop request %(name)s</b><br/>'
            '<b>Requested by:</b> %(requester)s<br/>'
            '<b>Client:</b> %(client)s<br/>'
            '<b>Vehicle:</b> %(vehicle)s<br/>'
            '<b>Description:</b> %(description)s'
        ) % {
            'name': self.name,
            'requester': self.employee_id.name,
            'client': client_text or '',
            'vehicle': vehicle_text or '',
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
