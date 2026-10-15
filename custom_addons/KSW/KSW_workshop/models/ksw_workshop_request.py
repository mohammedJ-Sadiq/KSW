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

# Keyword -> request_type. This is now the SEED for the nine
# ksw.workshop.request.type rows (data/request_types.xml) and the input to the
# 19.0.6.0.0 migration — it is no longer the live ruleset. The live keywords
# live on those records, where the workshop manager maintains them; read them
# from there, never from here.
#
# Ordering mattered when a request could only carry one type: the first match
# won, so the most specific/most common work went first. 872 of the 17,079
# descriptions match more than one of these, and every one of those lost the
# rest of its answer. request_type_ids (Many2many) keeps them all, which is
# why the ordering below is now only the display order of the tags.
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
    _STATE_FIELDS = {'state', 'rejection_reason', 'return_reason', 'completion_date'}
    _INTERNAL_NOTE_FIELDS = {'note', 'note_to_requester'}
    _REPORT_FIELDS = {
        'entry_datetime', 'exit_datetime', 'odometer_reading', 'tire_pressure',
        'tire_bolts', 'work_statement', 'repairs_parts', 'technician_id',
        'parts_cost', 'labor_cost', 'part_line_ids', 'labor_line_ids',
    }
    _REQUESTER_FIELDS = {
        'client_id', 'vehicle_type', 'vehicle_id', 'driver_id', 'description',
        'x_cash_customer_name', 'x_cash_vehicle_number', 'x_cash_tank_number',
        'x_cash_plate_number', 'x_cash_driver_name', 'x_cash_brand', 'x_cash_model', 'x_cash_year',
    }
    _MANAGER_ONLY_FIELDS = {'is_cash_customer'}
    # Tags are the workshop's own classification, not the requester's — they
    # are derived from his words, and corrected by the people who did the work.
    _CLASSIFICATION_FIELDS = {'request_type_ids'}

    name = fields.Char(default='New', copy=False, readonly=True, tracking=True)

    employee_id = fields.Many2one(
        'hr.employee', string='Requested By', required=True,
        default=lambda self: self.env.user.employee_id, readonly=True,
    )
    # Kept although it is no longer on the form: it is stored, and the
    # "Department" group-by in the search view measures on it. The other three
    # employee mirrors (work email, mobile, job title) were form-only and went
    # with the Requester section.
    department_id = fields.Many2one(related='employee_id.department_id', store=True, readonly=True)
    # Set when a Requester Assistant raises the request: the requester whose
    # trucks it is for. Written by create()/write() only, never by the client
    # (see _apply_on_behalf); the requester reads the request through it.
    x_on_behalf_of_id = fields.Many2one(
        'res.users', string='On Behalf Of', readonly=True, copy=False, index=True,
        help='The workshop requester this request was raised for by his assistant.')

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
    ], string='Request Type (legacy)', tracking=True)

    # What the request is about, as tags. Replaces the Selection above, which
    # is kept only as the provenance record of what each imported row was
    # classified as in Aug 2026 — it is on no view and nothing writes it.
    #
    # Nobody picks these: the requester types a description and the tags are
    # derived from it (_derive_request_types below). Asking a driver to place
    # his own fault in one of nine buckets is asking the wrong person, which
    # is why the field used to be wrong so often.
    request_type_ids = fields.Many2many(
        'ksw.workshop.request.type',
        'ksw_workshop_request_type_rel', 'request_id', 'type_id',
        string='Request Type', tracking=True)
    x_request_type_derived = fields.Boolean(
        string='Type Derived from Description', readonly=True, copy=False, default=True,
        help="True while the tags are the ones the keywords produced. Editing them by "
             "hand clears it, and from then on they are left alone — a correction must "
             "never be undone by the next edit to the description.")

    is_cash_customer = fields.Boolean(string='Cash Customer', tracking=True, copy=False)
    # Not compute_sudo: inside a sudo compute env.su is true for everybody, so
    # the env.su exemption below handed the checkbox to every requester.
    x_can_toggle_cash_customer = fields.Boolean(
        compute='_compute_x_can_toggle_cash_customer',
    )

    # A client is a res.partner carrying the customer role — Odoo's native
    # marker, the same one KSW_commissions and the BAS customer importer
    # already use. Without this domain the picker offered every contact in
    # the database, employees included. KSW's own fleet stays a valid client:
    # KSW_fleet stamps customer_rank=1 on env.company.partner_id.
    #
    # The customer role alone stopped being enough once the BAS sync landed:
    # 819 partners carry it and one of them owns a vehicle. So the picker is
    # narrowed a second time, to the clients the workshop manager registered
    # under Configuration -> Clients (ksw.workshop.client — a role assignment,
    # not a flag on the party). ksw.fleet.vehicle.client_id carries the exact
    # same domain, applied from this module; a test asserts they stay equal,
    # because a client pickable here whose vehicles are not pickable there
    # would be a dead end on this very form.
    client_id = fields.Many2one(
        'res.partner', string='Client', tracking=True,
        domain="[('customer_rank', '>', 0), ('x_workshop_client_ids.active', '=', True)]",
        default=lambda self: self.env.company.partner_id,
    )
    vehicle_type = fields.Selection([
        # The truck head, T###. Called "Trailer" (تريلا) because that is what
        # the workshop and the supervisors call it (a "Tractor Unit" label was
        # tried on 2026-10-14 and got mixed up with the tedder within hours).
        ('trailer', 'Trailer (تريلا)'),
        ('isuzu', 'Isuzu'),
        # The tanker semi-trailer the head pulls, carrying the 32 m³ tank:
        # «تيدر 68», the name BAS and the workshop both use.
        ('tank', 'Tedder (تيدر)'),
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
        ('returned', 'Returned to Requester'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        # History only since Oct 2026: the manager no longer rejects, he
        # returns the request to the requester with a reason (action_return).
        ('rejected', 'Rejected'),
    ], default='new', tracking=True, copy=False, required=True)

    note = fields.Text(string='Internal Note')
    note_to_requester = fields.Text(string='Note to Requester')
    rejection_reason = fields.Text()
    return_reason = fields.Text(string='Return Reason', copy=False)
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
    # --- Spare parts issued (pass-through inventory) ---
    part_line_ids = fields.One2many(
        'ksw.workshop.part.line', 'request_id', string='Spare Parts Used')
    part_lines_cost = fields.Float(
        string='Spare Parts (Listed)', compute='_compute_part_lines_cost', store=True,
        help="Sum of the spare parts listed on this repair.")
    # Deliberately keeps its column and all 17,079 imported values — only its
    # label narrowed. Splitting the total this way (rather than turning
    # parts_cost itself into the computed total) is what makes this change
    # migration-free.
    parts_cost = fields.Float(
        string='Other Parts Cost',
        help="Free-typed figure for uncatalogued or one-off items that were not "
             "listed above. This is where the legacy history's parts costs live.")
    labor_cost = fields.Float(string='Labor / Service Fee')
    # --- Labor / service performed ---
    # What was done and who did it, itemised the way spare parts are. The fee
    # itself stays the single figure above: the workshop itemises the work,
    # not a price per task.
    labor_line_ids = fields.One2many(
        'ksw.workshop.labor.line', 'request_id', string='Labor / Service Performed')
    total_cost = fields.Float(
        string='Total Cost', compute='_compute_total_cost', store=True,
        help="Listed spare parts, plus other parts, plus labor. Note the legacy "
             "history barely carries costs (878 rows have a parts cost, 4 have a "
             "labor cost), so this is only meaningful for requests recorded in Odoo.")

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

    # --- "This year" history panel (see _compute_history) ---
    x_vehicle_history_ids = fields.Many2many(
        'ksw.workshop.request', string='Same Vehicle (This Year)', compute='_compute_history')
    x_driver_history_ids = fields.Many2many(
        'ksw.workshop.request', string='Same Driver (This Year)', compute='_compute_history')
    x_vehicle_history_count = fields.Integer(
        string='Visits This Year', compute='_compute_history')
    x_driver_history_count = fields.Integer(
        string='Driver Jobs This Year', compute='_compute_history')
    x_vehicle_history_cost = fields.Float(
        string='Spent on This Vehicle', compute='_compute_history')

    @api.depends('technician_id', 'technician_id.name', 'x_legacy_technician_name')
    def _compute_technician_label(self):
        for request in self:
            request.technician_label = (
                request.technician_id.name or request.x_legacy_technician_name or False
            )

    @api.depends('part_line_ids.subtotal')
    def _compute_part_lines_cost(self):
        for request in self:
            request.part_lines_cost = sum(request.part_line_ids.mapped('subtotal'))

    @api.depends('parts_cost', 'part_lines_cost', 'labor_cost')
    def _compute_total_cost(self):
        for request in self:
            request.total_cost = (
                (request.parts_cost or 0.0)
                + (request.part_lines_cost or 0.0)
                + (request.labor_cost or 0.0)
            )

    @api.depends('completion_date', 'create_date')
    def _compute_duration_days(self):
        for request in self:
            if request.completion_date and request.create_date:
                request.duration_days = (request.completion_date - request.create_date).days
            else:
                request.duration_days = 0

    # How many past jobs the panel will show inline. The busiest vehicles in
    # the imported history run to a few hundred visits a year, and the point of
    # the panel is insight while triaging, not an archive — the "Workshop
    # Visits" stat button on the vehicle form is the uncapped route.
    _HISTORY_LIMIT = 100

    @api.depends('vehicle_id', 'driver_id', 'client_id', 'create_date')
    def _compute_history(self):
        """Past jobs on this vehicle, and for this driver under this client, this year.

        The workshop's equivalent of the Time Off form's "<employee>'s summary
        (2026)" panel. Computed with sudo() so the figures are complete: it is
        shown only to technicians and managers (the page carries the groups=),
        and a partial history that looks complete is worse than none.

        Not @api.depends_context('uid') on purpose — the result is the same for
        every user precisely because it is sudo'd, so sharing the cache entry
        across users is correct here.
        """
        today = fields.Date.context_today(self)
        Request = self.sudo()
        for request in self:
            # A brand-new form has a NewId, which no domain can express. This
            # guard is the only reason the panel does not crash on the one
            # screen it exists for.
            this_id = request.id if isinstance(request.id, int) else 0
            reference = request.create_date.date() if request.create_date else today
            year_start = reference.replace(month=1, day=1)
            year_end = reference.replace(month=12, day=31)
            common = [
                ('id', '!=', this_id),
                ('state', '!=', 'rejected'),
                ('create_date', '>=', fields.Datetime.to_datetime(year_start)),
                ('create_date', '<=', fields.Datetime.to_datetime(year_end).replace(
                    hour=23, minute=59, second=59)),
            ]

            vehicle_history = Request.browse()
            if request.vehicle_id:
                # A vehicle belongs to exactly one client, so "under this
                # client" is already implied here.
                vehicle_history = Request.search(
                    common + [('vehicle_id', '=', request.vehicle_id.id)],
                    order='create_date desc', limit=self._HISTORY_LIMIT,
                )

            driver_history = Request.browse()
            if request.driver_id and request.client_id:
                # The driver half does need the client stated: a driver can
                # move between clients, and mixing them would misread as one
                # relationship's history.
                driver_history = Request.search(
                    common + [
                        ('driver_id', '=', request.driver_id.id),
                        ('client_id', '=', request.client_id.id),
                    ],
                    order='create_date desc', limit=self._HISTORY_LIMIT,
                )

            request.x_vehicle_history_ids = vehicle_history
            request.x_driver_history_ids = driver_history
            request.x_vehicle_history_count = len(vehicle_history)
            request.x_driver_history_count = len(driver_history)
            request.x_vehicle_history_cost = sum(vehicle_history.mapped('total_cost'))

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
        if not self._driver_follows_vehicle():
            return
        for request in self:
            if request.vehicle_id.driver_id:
                request.driver_id = request.vehicle_id.driver_id

    @api.model
    def _workshop_driver_ids(self):
        """None = anyone; otherwise the employee ids the current user may name
        as a request's driver: his reporting line, himself included. An
        assistant gets his requesters' lines. No employee is an empty list,
        never the whole company."""
        user = self.env.user
        if self.env.su or user.has_group('base.group_system') \
                or user.has_group('KSW_workshop.group_workshop_manager'):
            return None
        managers = self.env['res.users'].browse(user._ksw_workshop_assisted_manager_ids())
        if managers:
            allowed = set()
            for manager in managers:
                scope = self.with_user(manager)._workshop_driver_ids()
                if scope is None:
                    return None
                allowed |= scope
            return allowed
        employee = user.sudo().employee_id
        if not employee:
            return set()
        Employee = self.env['hr.employee'].sudo().with_context(ksw_workshop_driver_picker=False)
        return set(Employee.search([('id', 'child_of', employee.id)]).ids)

    def _driver_from_vehicle(self, vehicle):
        """The driver picking this vehicle fills in, when the fill-in is on.
        The tanker bridge adds the BAS driver."""
        return vehicle.sudo().driver_id

    @api.model
    def _check_driver_allowed(self, driver_id, vehicle_id):
        if self.env.su or not driver_id:
            return
        allowed = self._workshop_driver_ids()
        if allowed is None or driver_id in allowed:
            return
        # The fill-in may name a driver from another team: the truck's own.
        vehicle = self.env['ksw.fleet.vehicle'].browse(vehicle_id)
        if vehicle_id and self._driver_follows_vehicle() \
                and driver_id in self._driver_from_vehicle(vehicle).ids:
            return
        raise UserError(_(
            '%s is not in your team. You can name as driver only yourself and '
            'the employees who report to you.',
            self.env['hr.employee'].sudo().browse(driver_id).name))

    @api.model
    def _driver_follows_vehicle(self):
        """Does picking a vehicle fill in its driver? Switched off (Oct 2026):
        the vehicle's recorded driver is not reliably who brings it in, so
        the requester names the driver himself. The fill-in code is kept for
        when the fleet's drivers are trusted; turn it back on with the system
        parameter KSW_workshop.driver_follows_vehicle = 1."""
        return self.env['ir.config_parameter'].sudo().get_param(
            'KSW_workshop.driver_follows_vehicle', '0') == '1'

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
                     'driver_id')
    def _check_vehicle_and_cash_customer(self):
        for request in self:
            if request.is_cash_customer:
                # Every Cash Customer detail is optional, by decision
                # (2026-09-14): a walk-in is written down with whatever the
                # person at the counter actually has, and the description —
                # required for every request — is the part that always exists.
                continue
            if not request.client_id or not request.vehicle_id:
                raise ValidationError(_(
                    'Client and Vehicle are required unless this is a Cash Customer request.'
                ))
            # Vehicle Type and Driver are required of anyone submitting a
            # normal request, but NOT retro-applied to the 17,079 imported
            # rows: the Google Sheet never had a driver column (exactly one of
            # them carries a driver), so enforcing it here would make the whole
            # legacy history uneditable the moment a manager corrected a
            # vehicle on it.
            if not request.x_imported and not (request.vehicle_type and request.driver_id):
                raise ValidationError(_(
                    'Vehicle Type and Driver are required unless this is a Cash '
                    'Customer request.'
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
    # Reviewer: read-only views
    # ------------------------------------------------------------------
    @api.model
    def _is_workshop_reviewer_only(self):
        """A Reviewer who holds no role that writes requests.

        A reviewer who is also a requester keeps his normal views: he still
        raises and edits his own requests, and anyone else's is refused by
        write() and the record rules anyway.
        """
        user = self.env.user
        return user.has_group('KSW_workshop.group_workshop_reviewer') and not any(
            user.has_group(xmlid) for xmlid in (
                'KSW_workshop.group_workshop_manager',
                'KSW_workshop.group_workshop_technician',
                'KSW_workshop.group_workshop_requester',
                'KSW_workshop.group_workshop_requester_assistant',
            ))

    @api.model
    def _get_view_cache_key(self, view_id=None, view_type='form', **options):
        key = super()._get_view_cache_key(view_id, view_type, **options)
        return key + (self._is_workshop_reviewer_only(),)

    @api.model
    def _get_view(self, view_id=None, view_type='form', **options):
        arch, view = super()._get_view(view_id, view_type, **options)
        # Without this the repair report reads as editable on an In Progress
        # request and the reviewer only learns otherwise when Save fails.
        if view_type in ('form', 'list', 'kanban') and self._is_workshop_reviewer_only():
            for attr in ('create', 'edit', 'delete'):
                arch.set(attr, '0')
        return arch, view

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        is_manager = self.env.su or self.env.user.has_group('KSW_workshop.group_workshop_manager')
        RequestType = self.env['ksw.workshop.request.type']
        for vals in vals_list:
            if not is_manager:
                vals['employee_id'] = self.env.user.employee_id.id
                vals['is_cash_customer'] = False
            if not vals.get('x_imported') and not vals.get('is_cash_customer'):
                self._check_driver_allowed(vals.get('driver_id'), vals.get('vehicle_id'))
            if not vals.get('name') or vals['name'] == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('ksw.workshop.request') or 'New'
            # Tags come from the words the requester wrote. An explicit list
            # (an import, a manager creating on someone's behalf) is left
            # alone and marked as chosen, so it is never re-derived away.
            if vals.get('request_type_ids'):
                vals['x_request_type_derived'] = False
            else:
                derived = RequestType._match_description(vals.get('description'))
                vals['request_type_ids'] = [fields.Command.set(derived.ids)]
                vals['x_request_type_derived'] = True
        assistant_managers = self._assistant_managers_or_raise()
        for vals in vals_list:
            if not self.env.su:
                vals.pop('x_on_behalf_of_id', None)
            if assistant_managers:
                vals['x_on_behalf_of_id'] = self._pick_on_behalf(
                    assistant_managers, vals.get('vehicle_id')).id
        requests = super().create(vals_list)
        for request in requests:
            request._notify_workshop_managers()
        return requests

    @api.model
    def _assistant_managers_or_raise(self):
        """The requesters the current user raises requests for, as assistant.

        Empty for everyone who raises his own (requesters, the workshop,
        sudo). An assistant who assists nobody cannot raise anything: the
        role alone is inert, and saying so beats an empty vehicle list.
        """
        user = self.env.user
        if self.env.su or not user.has_group('KSW_workshop.group_workshop_requester_assistant') \
                or user.has_group('KSW_workshop.group_workshop_requester') \
                or user.has_group('KSW_workshop.group_workshop_manager'):
            return self.env['res.users']
        managers = self.env['res.users'].browse(user._ksw_workshop_assisted_manager_ids())
        if not managers:
            raise UserError(_(
                'You raise workshop requests for your manager, but your manager is not '
                'a workshop requester and nobody else is named on your user. Ask an '
                'administrator to set this up.'))
        return managers

    @api.model
    def _pick_on_behalf(self, managers, vehicle_id):
        """Which of the assisted requesters a request is for. Without vehicle
        scopes (KSW_workshop_tanker) there is nothing to tell them apart by,
        so it is the assistant's own manager, who comes first."""
        return managers[:1]

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
        # Set only by _apply_derived_types() below, so the tagger's own write
        # is not mistaken for a person choosing the tags by hand.
        deriving = self.env.context.get('ksw_deriving_request_types')
        if not self.env.su and not deriving:
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
                    if request.employee_id.user_id != user or request.state not in ('new', 'returned'):
                        raise UserError(_(
                            'You can only edit your own request while it is New or '
                            'Returned to you.'))

            if touched & self._CLASSIFICATION_FIELDS and not (
                is_manager or user.has_group('KSW_workshop.group_workshop_technician')
            ):
                raise UserError(_(
                    'The request type is set from the description. Only the workshop '
                    'can change it.'
                ))

        # Never from the client. When an assistant moves his request to
        # another truck, it may now be for another of his requesters.
        if not self.env.su:
            vals = {k: v for k, v in vals.items() if k != 'x_on_behalf_of_id'}
        if 'vehicle_id' in vals:
            assistant_managers = self._assistant_managers_or_raise()
            if assistant_managers:
                vals['x_on_behalf_of_id'] = self._pick_on_behalf(
                    assistant_managers, vals['vehicle_id']).id

        # A person choosing the tags ends the derivation, permanently: a
        # correction must not be undone by the next edit to the description.
        if 'request_type_ids' in vals and not deriving:
            vals = dict(vals, x_request_type_derived=False)

        if vals.get('driver_id') and not self.env.su:
            for request in self.filtered(lambda r: r.driver_id.id != vals['driver_id']):
                request._check_driver_allowed(
                    vals['driver_id'], vals.get('vehicle_id', request.vehicle_id.id))

        res = super().write(vals)

        # Still New and still untouched by a person: the tags follow the words.
        if 'description' in vals and 'request_type_ids' not in vals:
            stale = self.filtered(
                lambda request: request.x_request_type_derived
                and request.state in ('new', 'returned'))
            stale._apply_derived_types()
        return res

    def _apply_derived_types(self):
        """Re-run the keyword rules over each request's own description.

        sudo() because the rules are configuration and the requester editing
        his own description is not the one being asked to classify it; the
        context flag is what tells write() above that this is the tagger, not
        a person.
        """
        RequestType = self.env['ksw.workshop.request.type']
        for request in self:
            derived = RequestType._match_description(request.description)
            if derived != request.request_type_ids:
                request.sudo().with_context(ksw_deriving_request_types=True).write({
                    'request_type_ids': [fields.Command.set(derived.ids)],
                })

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

    def action_open_return_wizard(self):
        self._check_manager()
        self.ensure_one()
        if self.state != 'new':
            raise UserError(_('Only new requests can be returned to the requester.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Return to Requester'),
            'res_model': 'ksw.workshop.return.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_request_id': self.id},
        }

    def action_return(self, reason):
        """Send a New request back to its requester to correct and resubmit.

        Replaces Reject (Oct 2026): the workshop manager may no longer close a
        request on his own; the requester decides what happens to it next.
        """
        self._check_manager()
        if not (reason or '').strip():
            raise UserError(_('Please give the reason for returning the request.'))
        for request in self:
            if request.state != 'new':
                raise UserError(_('Only new requests can be returned to the requester.'))
        self.write({'state': 'returned', 'return_reason': reason.strip()})
        for request in self:
            request._notify_requester(
                _('Your workshop request was returned to you. Please correct it and resubmit.'),
                reason=request.return_reason)

    def action_resubmit(self):
        user = self.env.user
        is_manager = self.env.su or user.has_group('KSW_workshop.group_workshop_manager')
        for request in self:
            if request.state != 'returned':
                raise UserError(_('Only returned requests can be resubmitted.'))
            if not is_manager and request.employee_id.user_id != user:
                raise UserError(_('Only the requester can resubmit this request.'))
        # sudo(): state is a manager field in write(); the requester was
        # authorised just above.
        self.sudo().write({'state': 'new'})
        for request in self:
            request._notify_workshop_managers(resubmitted=True)

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
    def _notify_workshop_managers(self, resubmitted=False):
        group = self.env.ref('KSW_workshop.group_workshop_manager', raise_if_not_found=False)
        # admin and OdooBot hold the group for system access, not as workshop
        # people: admin's address is the shared info@ mailbox, so every new
        # request used to land there.
        technical = self.env['res.users']
        for xmlid in ('base.user_admin', 'base.user_root'):
            technical |= self.env.ref(xmlid, raise_if_not_found=False) or self.env['res.users']
        users = (group.user_ids - technical) if group else self.env['res.users']
        partner_ids = users.mapped('partner_id').ids
        if not partner_ids:
            return
        client_text = self.x_cash_customer_name if self.is_cash_customer else self.client_id.display_name
        vehicle_text = self.x_cash_vehicle_number if self.is_cash_customer else self.vehicle_id.display_name
        body = Markup(
            '<b>%(title)s</b><br/>'
            '<b>Requested by:</b> %(requester)s<br/>'
            '<b>Client:</b> %(client)s<br/>'
            '<b>Vehicle:</b> %(vehicle)s<br/>'
            '<b>Description:</b> %(description)s'
        ) % {
            'title': (_('Workshop request %s resubmitted', self.name) if resubmitted
                      else _('New workshop request %s', self.name)),
            'requester': self.employee_id.name,
            'client': client_text or '',
            'vehicle': vehicle_text or '',
            'description': self.description,
        }
        self.sudo().message_post(body=body, partner_ids=partner_ids, subtype_xmlid='mail.mt_comment')

    def _notify_requester(self, instruction, reason=None):
        self.ensure_one()
        partner = self.employee_id.user_id.partner_id
        if not partner:
            return
        body = Markup('<b>%(instruction)s</b>') % {'instruction': instruction}
        if reason:
            body += Markup('<br/><b>%(label)s</b> %(reason)s') % {
                'label': _('Reason:'), 'reason': reason}
        if self.note_to_requester:
            body += Markup('<br/>%(note)s') % {'note': self.note_to_requester}
        self.sudo().message_post(body=body, partner_ids=[partner.id], subtype_xmlid='mail.mt_comment')
