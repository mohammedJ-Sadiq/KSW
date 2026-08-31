from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

# Suffix stamped on the bare hr.employee records the legacy history import
# creates for requesters who never had an Odoo account (scripts/import_history.py).
# They exist only to satisfy ksw.workshop.request.employee_id (required) — the
# real submitted name and email live on the request itself in
# x_legacy_requester_name / x_legacy_requester_email, so nothing depends on
# them being visible. They are created archived and the 19.0.7.0.0 migration
# archives the ones already in the database: 83 of them were inflating the
# employee headcount and turning up in every employee picker.
LEGACY_PLACEHOLDER_SUFFIX = ' (Legacy Import - No Odoo Account)'

# Keyword -> request_type, used to classify the legacy Google-Sheet history,
# whose `request_type` was empty on all 17,079 rows (the sheet never had the
# column). Ordered: the FIRST entry whose pattern matches wins, so the most
# specific/most common work goes first — ~400 rows mention more than one of
# these and would otherwise classify arbitrarily.
#
# Patterns are POSIX regex fragments fed to SQL `~*` by the 19.0.6.0.0
# migration, so they must stay valid PostgreSQL regexes. Arabic misspellings
# are deliberate: `زبت` for `زيت` and `chance oil` for `change oil` are how the
# supervisors actually typed them, and together they are hundreds of rows.
#
# Coverage on the KSWCO corpus is ~74%; the remaining ~26% is a long tail of
# one-off free text and is deliberately left NULL ("Unclassified") rather than
# swept into a catch-all, so a report never implies a classification nobody made.
REQUEST_TYPE_KEYWORDS = [
    ('oil_filters', r'زيت|زبت|فلتر|فلاتر|change oil|chance oil|chang oil|oil change'),
    ('tyres', r'اطار|إطار|كفر|بنشر|كاوتش'),
    ('brakes', r'فرامل|بريك|قماشات'),
    ('electrical', r'كهرب|دينامو|بطاري|سلف|مولد'),
    ('bodywork', r'لحام|سمكر|دهان|بوية'),
    ('water_pump', r'موتور|طرمبة|مضخة'),
    ('hoses', r'هوز|خرطوم'),
    ('mechanical', r'كلتش|جير|دبرياج|عفشة|رديتر|مكيف|دنجل'),
    ('inspection', r'فحص دوري|الفحص الدوري|استمارة'),
]


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

    # The four original values keep their keys; the rest were added once the
    # 17k imported descriptions showed what the workshop actually does — tyres
    # and brakes alone are ~2,150 requests that used to collapse into
    # "Mechanical". See REQUEST_TYPE_KEYWORDS below for how history was
    # classified.
    request_type = fields.Selection([
        ('oil_filters', 'Oil & Filters'),
        ('tyres', 'Tyres'),
        ('brakes', 'Brakes'),
        ('electrical', 'Electrical'),
        ('bodywork', 'Bodywork / Welding'),
        ('water_pump', 'Water Pump / Motor'),
        ('hoses', 'Hoses'),
        ('mechanical', 'Mechanical'),
        ('inspection', 'Periodic Inspection'),
    ], string='Request Type', tracking=True)
    x_request_type_derived = fields.Boolean(
        string='Type Derived from Description', readonly=True, copy=False,
        help="Set by the history backfill when the request type was inferred from the "
             "description text rather than chosen by a person. Lets a report separate "
             "derived values from entered ones.")

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
    # store=True so it can be used as a pivot/graph measure — an unstored
    # compute cannot be grouped, sorted or measured. Same reason as
    # helpdesk.ticket.resolution_hours.
    duration_days = fields.Integer(compute='_compute_duration_days', store=True)

    # --- Repair report (workshop technician / manager only) ---
    entry_datetime = fields.Datetime(string='Entry Date/Time')
    exit_datetime = fields.Datetime(string='Exit Date/Time')
    odometer_reading = fields.Integer(string='Odometer Reading')
    tire_pressure = fields.Char(string='Tire Pressure')
    tire_bolts = fields.Char(string='Tire Bolts')
    work_statement = fields.Text(string='Statement of Required Work')
    repairs_parts = fields.Text(string='Repairs & Spare Parts')
    technician_id = fields.Many2one('hr.employee', string='Technician')
    x_legacy_technician_name = fields.Char(
        string='Technician Name (as recorded)', readonly=True, copy=False,
        help="The technician name written on the legacy sheet. Those names are bare "
             "first names that match no single hr.employee, so they could not become "
             "technician_id — this keeps them rather than losing them.")
    technician_label = fields.Char(
        string='Technician (report)', compute='_compute_technician_label', store=True,
        help="Single grouping axis for technician reporting: the linked employee for "
             "new requests, the legacy name for imported history.")
    parts_cost = fields.Float(string='Spare Parts Cost')
    labor_cost = fields.Float(string='Labor Cost')
    total_cost = fields.Float(
        string='Total Cost', compute='_compute_total_cost', store=True,
        help="Spare parts plus labor. Note the legacy history barely carries costs "
             "(878 rows have a parts cost, 4 have a labor cost), so this is only "
             "meaningful for requests recorded in Odoo.")

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

    @api.depends('technician_id', 'technician_id.name', 'x_legacy_technician_name')
    def _compute_technician_label(self):
        for request in self:
            request.technician_label = (
                request.technician_id.name or request.x_legacy_technician_name or False
            )

    @api.depends('parts_cost', 'labor_cost')
    def _compute_total_cost(self):
        for request in self:
            request.total_cost = (request.parts_cost or 0.0) + (request.labor_cost or 0.0)

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

        Single source of truth: called from write() below — kept as its own
        method so it stays the one place this rule is expressed.
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
        # instead of this method's clean UserError.
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
