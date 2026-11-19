from datetime import timedelta

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.fields import Domain

from .plates import plate_key, vin_key

OFFICER_GROUP = 'KSW_documents.group_document_officer'
# A status-only confirmation from a monthly export stays good this long.
VERIFIED_DAYS = 45


# A plate is the same plate in Arabic or Latin letters, any order or spacing.
_SAME_IF = {'plate_number': plate_key, 'x_vin': vin_key}


class KswDocument(models.Model):
    """One document of a vehicle or an employee. Uploaded (and possibly read
    by AI) it is a draft; HR confirms it, which replaces the previous one of
    the same type for the same owner."""
    _name = 'ksw.document'
    _description = 'Document'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'expiry_date, id desc'

    name = fields.Char(compute='_compute_name', store=True)
    type_id = fields.Many2one('ksw.document.type', string='Document', required=True, index=True,
                              tracking=True, domain="[('applies_to', '=', applies_to)]")
    applies_to = fields.Selection([('vehicle', 'Vehicle'), ('employee', 'Employee'), ('company', 'Company')],
                                  required=True, default='vehicle')
    company_id = fields.Many2one('res.company', string='Company', index=True, ondelete='cascade')
    vehicle_id = fields.Many2one('ksw.fleet.vehicle', string='Vehicle', index=True, ondelete='cascade')
    employee_id = fields.Many2one('hr.employee', string='Employee', index=True, ondelete='cascade')
    number = fields.Char(tracking=True)
    issue_date = fields.Date(tracking=True)
    expiry_date = fields.Date(tracking=True, index=True)
    license_class = fields.Char(string='Class', tracking=True)
    has_expiry = fields.Boolean(related='type_id.has_expiry')
    has_class = fields.Boolean(related='type_id.has_class')
    attachment_ids = fields.Many2many('ir.attachment', 'ksw_document_attachment_rel', 'document_id',
                                      'attachment_id', string='Scan')
    state = fields.Selection([
        ('draft', 'To Confirm'),
        ('confirmed', 'Current'),
        ('replaced', 'Replaced'),
    ], default='draft', required=True, tracking=True, index=True, copy=False)
    expiry_status = fields.Selection([
        ('valid', 'Valid'),
        ('expiring', 'Expiring Soon'),
        ('expired', 'Expired'),
        ('unknown', 'Expiry Not Entered'),
        ('none', 'No Expiry'),
    ], compute='_compute_expiry_status', search='_search_expiry_status', string='Validity')
    days_left = fields.Integer(compute='_compute_expiry_status')
    ai_read = fields.Boolean(string='Read by AI', readonly=True, copy=False)
    ai_notes = fields.Text(string='Check Before Confirming', readonly=True, copy=False,
                           help='What the AI reader was unsure of, or what does not match the owner.')
    confirmed_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    confirmed_date = fields.Datetime(readonly=True, copy=False)
    # Bulk upload: what the document says about its owner, used to find him
    # (and, once confirmed, to fill what the vehicle / employee record lacks).
    registration_id = fields.Many2one('ksw.company.registration', string='Registration (CR)', index=True,
                                      ondelete='restrict', tracking=True,
                                      domain="[('company_id', '=', company_id)]",
                                      help='Which of the company\'s registrations (main CR, branch, group company) '
                                           'this document is for.')
    detected_parent_number = fields.Char(string='Main CR No. (as read)')
    source = fields.Selection([('upload', 'Scan'), ('absher', 'Absher Business'), ('muqeem', 'Muqeem'),
                               ('naql', 'Naql'), ('qiwa', 'Qiwa'), ('fahes', 'Fahes (tank inspection)')], default='upload', required=True, readonly=True,
                              string='Source')
    verified_date = fields.Date(readonly=True, help='The day a government export last showed this document as '
                                                    'valid. Used when the export gives a status but no date.')
    import_id = fields.Many2one('ksw.document.import', string='Import', readonly=True, ondelete='set null',
                                index=True)
    batch_id = fields.Many2one('ksw.document.batch', string='Bulk Upload', index=True, ondelete='set null')
    detected_plate = fields.Char(string='Plate (as read)')
    detected_vin = fields.Char(string='Chassis No. (as read)')
    detected_id_number = fields.Char(string='ID / Iqama No. (as read)')
    detected_name = fields.Char(string='Name (as read)')
    detected_nationality = fields.Char(string='Nationality (as read)')
    detected_make = fields.Char(string='Make (as read)')
    detected_model = fields.Char(string='Model (as read)')
    detected_year = fields.Integer(string='Year (as read)')
    detected_color = fields.Char(string='Colour (as read)')
    match_note = fields.Char(string='Found By', readonly=True)
    x_is_pending_my_action = fields.Boolean(compute='_compute_is_pending_my_action',
                                            search='_search_is_pending_my_action', string='Waiting for Me')

    @api.depends('type_id', 'number', 'vehicle_id', 'employee_id')
    def _compute_name(self):
        for doc in self:
            owner = doc.vehicle_id.name or doc.employee_id.name or ''
            doc.name = ' – '.join(p for p in (doc.type_id.name, doc.number, owner) if p)

    @api.depends('expiry_date', 'type_id.alert_days', 'type_id.has_expiry')
    def _compute_expiry_status(self):
        today = fields.Date.context_today(self)
        for doc in self:
            if not doc.type_id.has_expiry:
                doc.expiry_status, doc.days_left = 'none', 0
                continue
            if not doc.expiry_date:
                # A portal said "valid" without a date: valid while that is recent.
                fresh = doc.verified_date and (today - doc.verified_date).days <= VERIFIED_DAYS
                doc.expiry_status, doc.days_left = ('valid' if fresh else 'unknown'), 0
                continue
            doc.days_left = (doc.expiry_date - today).days
            doc.expiry_status = 'expired' if doc.days_left < 0 else (
                'expiring' if doc.days_left <= doc.type_id.alert_days else 'valid')

    def _search_expiry_status(self, operator, value):
        if operator not in ('=', 'in'):
            return NotImplemented
        values = value if operator == 'in' else [value]
        today = fields.Date.context_today(self)
        parts = []
        for status in values:
            if status == 'expired':
                parts.append([('expiry_date', '<', today), ('type_id.has_expiry', '=', True)])
            elif status == 'expiring':
                # Alert windows differ per type: one condition per window.
                for days in set(self.env['ksw.document.type'].search([]).mapped('alert_days')):
                    parts.append([('type_id.alert_days', '=', days), ('type_id.has_expiry', '=', True),
                                  ('expiry_date', '>=', today),
                                  ('expiry_date', '<=', today + timedelta(days=days))])
            elif status == 'valid':
                parts.append([('type_id.has_expiry', '=', True), ('expiry_date', '=', False),
                              ('verified_date', '>=', today - timedelta(days=VERIFIED_DAYS))])
                for days in set(self.env['ksw.document.type'].search([]).mapped('alert_days')):
                    parts.append([('type_id.alert_days', '=', days), ('type_id.has_expiry', '=', True),
                                  ('expiry_date', '>', today + timedelta(days=days))])
            elif status == 'none':
                parts.append([('type_id.has_expiry', '=', False)])
            elif status == 'unknown':
                parts.append(Domain([('type_id.has_expiry', '=', True), ('expiry_date', '=', False)]) & Domain.OR([
                    [('verified_date', '=', False)],
                    [('verified_date', '<', today - timedelta(days=VERIFIED_DAYS))]]))
        return Domain.OR(parts) if parts else [('id', '=', False)]

    @api.model
    def _pending_domain(self):
        if not (self.env.su or self.env.user.has_group(OFFICER_GROUP)):
            return Domain([('id', '=', False)])
        return Domain.OR([[('state', '=', 'draft')],
                          Domain([('state', '=', 'confirmed')]) & Domain(
                              self._search_expiry_status('in', ['expiring', 'expired', 'unknown']))])

    @api.depends_context('uid')
    def _compute_is_pending_my_action(self):
        mine = set(self.search(self._pending_domain()).ids)
        for doc in self:
            doc.x_is_pending_my_action = doc.id in mine

    def _search_is_pending_my_action(self, operator, value):
        if operator in ('in', 'not in'):
            wanted = (operator == 'in') == any(value)
        elif operator in ('=', '!='):
            wanted = (operator == '=') == bool(value)
        else:
            return NotImplemented
        domain = self._pending_domain()
        return domain if wanted else ~domain

    @api.constrains('applies_to', 'vehicle_id', 'employee_id', 'company_id', 'type_id', 'state')
    def _check_owner(self):
        owners = {'vehicle': 'vehicle_id', 'employee': 'employee_id', 'company': 'company_id'}
        for doc in self:
            others = [f for k, f in owners.items() if k != doc.applies_to and doc[f]]
            if others:
                raise ValidationError(_('A document belongs to one owner.'))
            # A bulk-upload draft may wait for HR to say whose it is.
            if not doc[owners[doc.applies_to]] and not (doc.state == 'draft' and doc.batch_id):
                raise ValidationError(_('Choose whose document %s is.', doc.type_id.name or ''))
            if doc.type_id and doc.type_id.applies_to != doc.applies_to:
                raise ValidationError(_('%s is not a document of this kind of owner.', doc.type_id.name))

    def write(self, vals):
        locked = {'type_id', 'number', 'issue_date', 'expiry_date', 'license_class',
                  'vehicle_id', 'employee_id', 'company_id', 'applies_to'}
        # An expiry the card does not print (istimara) is added later from
        # Absher: filling an empty expiry is the one change a current
        # document takes.
        if set(vals) == {'expiry_date'} and not self.filtered('expiry_date'):
            locked = set()
        if not self.env.su and self.filtered(lambda d: d.state != 'draft') and set(vals) & locked:
            raise UserError(_('A confirmed document is not edited: upload the renewed one, which '
                              'replaces it when confirmed.'))
        res = super().write(vals)
        # HR chose whose a bulk-upload document is: the batch's other
        # documents of the same vehicle or person follow.
        if {'vehicle_id', 'employee_id'} & set(vals) and not self.env.context.get('ksw_document_grouping'):
            self.batch_id._group_owners(source=self.filtered(lambda d: d.state == 'draft'))
        return res

    # ------------------------------------------------------------------
    def action_confirm(self):
        """HR confirms what the scan says. The previous document of the same
        type for the same owner is replaced; an employee's iqama, passport or
        visa is written back to Odoo's own fields."""
        if not (self.env.su or self.env.user.has_group(OFFICER_GROUP)):
            raise UserError(_('Only HR confirms a document.'))
        for doc in self:
            if doc.state != 'draft':
                raise UserError(_('%s is already confirmed.', doc.name))
            if not (doc.vehicle_id or doc.employee_id or doc.company_id):
                raise UserError(_('Choose whose document %s is.', doc.name))
            if not doc.number:
                raise UserError(_('Fill in the document number of %s.', doc.name))
            if doc.company_id and not doc.registration_id and doc.type_id.code != 'commercial_registration':
                raise UserError(_('Choose which registration (main CR or branch) %s is for.', doc.name))
            if doc.type_id.has_expiry and doc.type_id.expiry_on_document and not doc.expiry_date:
                raise UserError(_('Fill in the expiry date of %s.', doc.name))
            # No scan needed (user decision 2026-11-19): the number and the
            # dates are what the register is for; a copy is optional.
        for doc in self.sudo():
            current = doc._owner_documents().filtered(
                lambda d: d.state == 'confirmed' and d.type_id == doc.type_id and d != doc)
            newer = current.filtered(lambda d: d.expiry_date and doc.expiry_date and d.expiry_date > doc.expiry_date)
            if newer:
                # An old paper uploaded late is history: it never replaces the valid one.
                doc.write({'state': 'replaced', 'confirmed_by_id': self.env.user.id,
                           'confirmed_date': fields.Datetime.now()})
                doc.message_post(body=_('Filed as history: %(d)s (valid to %(e)s) is newer.', d=newer[0].name,
                                        e=newer[0].expiry_date), subtype_xmlid='mail.mt_note')
                doc.attachment_ids.write({'res_model': doc._name, 'res_id': doc.id})
                continue
            current.write({'state': 'replaced'})
            doc.write({'state': 'confirmed', 'confirmed_by_id': self.env.user.id,
                       'confirmed_date': fields.Datetime.now()})
            doc.attachment_ids.write({'res_model': doc._name, 'res_id': doc.id})
            doc._sync_employee_fields()
            doc._fill_owner_master()
            doc._sync_registration()
        self._sync_activities()
        return True

    def _sync_registration(self):
        """A confirmed CR certificate is the registration: it is created (or
        completed) from what the certificate says."""
        self.ensure_one()
        if self.type_id.code != 'commercial_registration' or not self.company_id:
            return
        Registration = self.env['ksw.company.registration'].sudo()
        reg = self.registration_id or Registration._find(self.detected_id_number)
        parent = Registration._find(self.detected_parent_number).filtered(lambda r: r.kind == 'main')
        unified = self.detected_id_number or self.number
        cr = self.number if self.number and self.number != unified else False
        if not reg:
            reg = Registration.create({
                'company_id': self.company_id.id, 'name': self.detected_name or self.number,
                'unified_number': unified, 'cr_number': cr, 'issue_date': self.issue_date,
                'kind': 'branch' if parent else 'main' if not Registration.search(
                    [('company_id', '=', self.company_id.id), ('kind', '=', 'main')]) else 'branch',
                'parent_id': parent.id,
            })
        else:
            reg.write({k: v for k, v in {'cr_number': cr, 'issue_date': self.issue_date,
                                         'parent_id': parent.id}.items() if v and not reg[k]})
        if self.registration_id != reg:
            self.registration_id = reg

    @api.model
    def _record_from_source(self, doc_type, owner, source, import_rec=False, number=False, expiry=False,
                            verified=False, note=False, issue_date=False):
        """What a government export says about one document of one owner.
        Returns 'new', 'renewed', 'same' or 'older'. The export is the
        authority, so no scan and no HR confirmation are needed; it never
        moves a document backwards."""
        field = {'ksw.fleet.vehicle': 'vehicle_id', 'hr.employee': 'employee_id'}[owner._name]
        Document = self.sudo()
        current = Document.search([('type_id', '=', doc_type.id), (field, '=', owner.id),
                                   ('state', '=', 'confirmed')], order='expiry_date desc nulls last, id desc')
        top = current[:1]
        today = fields.Date.context_today(self)
        if top and (top.number or False) == (number or False) and top.expiry_date == (expiry or False):
            if verified:
                top.verified_date = today
            return 'same'
        if top and top.expiry_date and expiry and top.expiry_date > expiry:
            return 'older'
        doc = Document.create({
            'type_id': doc_type.id, 'applies_to': doc_type.applies_to, field: owner.id, 'number': number,
            'expiry_date': expiry, 'source': source, 'import_id': import_rec and import_rec.id,
            'verified_date': today if verified else False, 'ai_notes': note, 'issue_date': issue_date,
        })
        current.write({'state': 'replaced'})
        doc.write({'state': 'confirmed', 'confirmed_by_id': self.env.user.id,
                   'confirmed_date': fields.Datetime.now()})
        doc._sync_employee_fields()
        doc._sync_activities()
        current._sync_activities()
        return 'renewed' if top else 'new'

    def _owner_documents(self):
        self.ensure_one()
        # A company's documents are per registration: the Jubail CR does not
        # replace the transport branch's.
        return self.search([('vehicle_id', '=', self.vehicle_id.id), ('employee_id', '=', self.employee_id.id),
                            ('company_id', '=', self.company_id.id),
                            ('registration_id', '=', self.registration_id.id)])

    def _fill_owner_master(self):
        """What the confirmed document says about its owner fills what the
        owner's record lacks (plate, chassis, make... of a vehicle; nationality
        of an employee). A value already there is never overwritten: a
        difference is noted on the document for HR to look at."""
        self.ensure_one()
        if self.vehicle_id:
            record, pairs = self.vehicle_id.sudo(), [
                ('plate_number', self.detected_plate), ('x_vin', self.detected_vin),
                ('brand', self.detected_make), ('model', self.detected_model),
                ('year', self.detected_year), ('x_color', self.detected_color)]
        elif self.employee_id:
            record = self.employee_id.sudo()
            country = self._find_country(self.detected_nationality)
            pairs = [('country_id', country)]
        else:
            return
        vals, differences = {}, []
        for field, value in pairs:
            if not value:
                continue
            current = record[field]
            if not current:
                vals[field] = value.id if hasattr(value, 'id') else value
            elif field in _SAME_IF and _SAME_IF[field](current) == _SAME_IF[field](value):
                continue  # the same plate / chassis written another way
            elif (current.id if hasattr(current, 'id') else current) != (value.id if hasattr(value, 'id') else value):
                differences.append('%s: %s ≠ %s' % (record._fields[field].string,
                                                   getattr(current, 'display_name', current),
                                                   getattr(value, 'display_name', value)))
        if vals:
            record.write(vals)
            self.message_post(body=_('Filled on %(o)s: %(f)s', o=record.display_name,
                                     f=', '.join(record._fields[f].string for f in vals)),
                              subtype_xmlid='mail.mt_note')
        if differences:
            self.message_post(body=_('Differs from %(o)s (not changed): %(d)s', o=record.display_name,
                                     d='; '.join(differences)), subtype_xmlid='mail.mt_note')

    @api.model
    def _find_country(self, name):
        if not name:
            return self.env['res.country']
        Country = self.env['res.country'].sudo()
        for lang in ('en_US', 'ar_001'):
            country = Country.with_context(lang=lang).search([('name', '=ilike', name.strip())], limit=1)
            if country:
                return country
        return Country.with_context(lang='en_US').search([('name', 'ilike', name.strip())], limit=1)

    # Numbers that never change over a renewal (an iqama keeps its number;
    # payroll's WPS file reads it): a different one is a wrong match, not news.
    _STABLE_NUMBER_FIELDS = ('ssnid', 'identification_id')

    def _sync_employee_fields(self):
        for doc in self.filtered(lambda d: d.employee_id and d.state == 'confirmed'):
            vals = {}
            field = doc.type_id.sync_number_field
            if field and doc.number:
                current = doc.employee_id.sudo()[field]
                if field in self._STABLE_NUMBER_FIELDS and current and current != doc.number:
                    doc.message_post(body=_('%(e)s already has %(f)s %(c)s; this document says %(n)s. Not '
                                            'changed: check that the document is his.',
                                            e=doc.employee_id.name, f=doc.employee_id._fields[field].string,
                                            c=current, n=doc.number), subtype_xmlid='mail.mt_note')
                else:
                    vals[field] = doc.number
            if doc.type_id.sync_expiry_field and doc.expiry_date:
                vals[doc.type_id.sync_expiry_field] = doc.expiry_date
            if vals:
                doc.employee_id.sudo().write(vals)

    def action_open_scan(self):
        self.ensure_one()
        if not self.attachment_ids:
            raise UserError(_('No scan attached.'))
        return {'type': 'ir.actions.act_url', 'target': 'new',
                'url': '/web/content/%s?download=false' % self.attachment_ids[:1].id}

    # ------------------------------------------------------------------
    # Alerts: a to-do for HR per current document expiring or expired
    # ------------------------------------------------------------------
    @api.model
    def _cron_document_alerts(self):
        self.search([('state', '=', 'confirmed')])._sync_activities()
        # Replaced or draft ones keep no expiry to-do.
        self.search([('state', '!=', 'confirmed'), ('activity_ids', '!=', False)])._sync_activities()

    def _sync_activities(self):
        todo = self.env.ref('KSW_documents.mail_activity_document')
        technical = self.env.ref('base.user_admin') | self.env.ref('base.user_root')
        officers = self.env.ref(OFFICER_GROUP).sudo().all_user_ids.filtered(
            lambda u: u.active and not u.share) - technical
        for doc in self.sudo().exists():
            wanted = {}
            if doc.state == 'confirmed' and doc.expiry_status in ('expiring', 'expired'):
                summary = (_('Expired %(d)s: %(n)s', d=doc.expiry_date, n=doc.name)
                           if doc.expiry_status == 'expired'
                           else _('Expires %(d)s: %(n)s', d=doc.expiry_date, n=doc.name))
                wanted = {u.id: summary for u in officers}
            for activity in doc.activity_ids.filtered(lambda a: a.activity_type_id == todo):
                if wanted.get(activity.user_id.id) == activity.summary:
                    wanted.pop(activity.user_id.id)
                else:
                    activity.unlink()
            for user_id, summary in wanted.items():
                doc.activity_schedule(activity_type_id=todo.id, user_id=user_id, summary=summary,
                                      date_deadline=max(doc.expiry_date, fields.Date.context_today(self)))
