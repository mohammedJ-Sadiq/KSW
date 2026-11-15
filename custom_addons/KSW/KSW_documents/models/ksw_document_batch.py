import logging
import re

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .ksw_document import OFFICER_GROUP
from .plates import plate_key, vin_key

_logger = logging.getLogger(__name__)
_DIGITS = str.maketrans('٠١٢٣٤٥٦٧٨٩', '0123456789')


class KswDocumentBatch(models.Model):
    """Drop many scans at once; Claude reads them, Odoo finds whose each
    document is (chassis, plate, iqama number, name), documents of the same
    vehicle or person are grouped, and HR confirms what is right.

    Finding the owner gets better as it is used: a confirmed istimara fills
    the vehicle's plate and chassis number, an iqama the employee's number,
    so the next documents of the same vehicle or person are found alone."""
    _name = 'ksw.document.batch'
    _description = 'Bulk Document Upload'
    _inherit = ['mail.thread']
    _order = 'id desc'

    name = fields.Char(default=lambda self: _('Bulk upload %s', fields.Date.context_today(self)), required=True)
    attachment_ids = fields.Many2many('ir.attachment', 'ksw_document_batch_attachment_rel', 'batch_id',
                                      'attachment_id', string='Scans, Photos, Screenshots')
    read_attachment_ids = fields.Many2many('ir.attachment', 'ksw_document_batch_read_rel', 'batch_id',
                                           'attachment_id', string='Already Read', readonly=True)
    document_ids = fields.One2many('ksw.document', 'batch_id', string='Documents Found')
    errors = fields.Text(readonly=True)
    info = fields.Text(string='Not Added Again', readonly=True)
    unread_count = fields.Integer(compute='_compute_counts')
    found_count = fields.Integer(compute='_compute_counts')
    unmatched_count = fields.Integer(compute='_compute_counts')
    draft_count = fields.Integer(compute='_compute_counts')

    @api.depends('attachment_ids', 'read_attachment_ids', 'document_ids.state', 'document_ids.vehicle_id',
                 'document_ids.employee_id', 'document_ids.company_id')
    def _compute_counts(self):
        for batch in self:
            docs = batch.document_ids
            batch.unread_count = len(batch.attachment_ids - batch.read_attachment_ids)
            batch.found_count = len(docs)
            batch.draft_count = len(docs.filtered(lambda d: d.state == 'draft'))
            batch.unmatched_count = len(docs.filtered(
                lambda d: d.state == 'draft' and not (d.vehicle_id or d.employee_id or d.company_id)))

    def _check_officer(self):
        if not (self.env.su or self.env.user.has_group(OFFICER_GROUP)):
            raise UserError(_('Only HR uploads documents.'))

    # ------------------------------------------------------------------
    def action_read(self):
        """Read every file not read yet. One file failing does not stop the
        others: its error is listed and the rest go on."""
        self._check_officer()
        Reader = self.env['ksw.document.ai.reader']
        if not Reader._is_available():
            raise UserError(_('Reading documents needs Claude: set Settings ▸ AI Provider to Anthropic '
                              'Claude and add its API key.'))
        types = self.env['ksw.document.type'].search([])
        for batch in self:
            errors = []
            for attachment in (batch.attachment_ids - batch.read_attachment_ids).sudo():
                try:
                    with self.env.cr.savepoint():
                        for found in Reader._read_all(attachment, types):
                            batch._add_document(attachment, found)
                        batch.read_attachment_ids = [(4, attachment.id)]
                except UserError as exc:
                    errors.append('%s: %s' % (attachment.name, exc.args[0]))
            batch.errors = '\n'.join(errors) or False
            batch._group_owners()
        return True

    def _add_document(self, attachment, found):
        self.ensure_one()
        notes = found.pop('notes', [])
        who = found.pop('_who', {})
        found.pop('_holder', None)
        found.pop('_plate', None)
        doc_type = self.env['ksw.document.type'].browse(found.get('type_id'))
        applies_to = doc_type.applies_to or 'vehicle'
        owner, how = self._match_owner(applies_to, dict(who, _number=found.get('number')))
        same = self._same_document(doc_type, found.get('number'), found.get('expiry_date'), owner)
        if same:
            # Printed on more than one scan (the company licence is on every
            # operation card), or already in the register: once is enough.
            if same.state == 'draft':
                same.sudo().attachment_ids = [(4, attachment.id)]
            self.info = '\n'.join(filter(None, [self.info, _(
                '%(f)s: %(t)s %(n)s is already listed (%(d)s), not added again.',
                f=attachment.name, t=doc_type.name, n=found.get('number'),
                d=same.batch_id == self and _('this upload') or _('in the register'))]))
            return same
        vals = dict(found, **who)
        vals.update({
            'batch_id': self.id, 'applies_to': applies_to, 'ai_read': True,
            'attachment_ids': [(6, 0, attachment.ids)],
            'type_id': doc_type.id or self.env['ksw.document.type'].search([], limit=1).id,
            'match_note': how,
        })
        if applies_to == 'company' and owner:
            vals['registration_id'] = (self.env['ksw.company.registration']._find(who.get('detected_id_number'))
                                       or self.env['ksw.company.registration']._find(found.get('number'))).id
        if owner:
            vals[{'vehicle': 'vehicle_id', 'employee': 'employee_id', 'company': 'company_id'}[applies_to]] = owner.id
        elif applies_to == 'company' and who.get('detected_id_number'):
            notes.append(_('Issued to %(n)s (%(r)s): no registration in Odoo has this number. Add it under '
                           'Documents ▸ Configuration ▸ Commercial Registrations (or upload its CR certificate), '
                           'or choose the company and registration.',
                           n=who.get('detected_name') or '?', r=who['detected_id_number']))
        else:
            notes.append(_('Whose document this is was not found: choose it.'))
        vals['ai_notes'] = '\n'.join(f'• {n}' for n in notes) or False
        return self.env['ksw.document'].sudo().create(vals)

    def _same_document(self, doc_type, number, expiry, owner):
        """The same document already in this upload, or confirmed in the register."""
        if not (doc_type and number):
            return self.env['ksw.document']
        key = re.sub(r'[^0-9A-Z]', '', str(number).upper())
        Document = self.env['ksw.document'].sudo()
        here = self.document_ids.filtered(lambda d: d.type_id == doc_type and d.state == 'draft'
                                          and re.sub(r'[^0-9A-Z]', '', (d.number or '').upper()) == key
                                          and (not expiry or not d.expiry_date or d.expiry_date == expiry))
        if here:
            return here[:1]
        if owner:
            field = {'ksw.fleet.vehicle': 'vehicle_id', 'hr.employee': 'employee_id',
                     'res.company': 'company_id'}[owner._name]
            known = Document.search([('type_id', '=', doc_type.id), (field, '=', owner.id),
                                     ('state', '=', 'confirmed')]).filtered(
                lambda d: re.sub(r'[^0-9A-Z]', '', (d.number or '').upper()) == key
                and (not expiry or d.expiry_date == expiry))
            return known[:1]
        return Document

    @api.model
    def _match_owner(self, applies_to, who):
        """(owner, how found) from what the document says, most certain first."""
        if applies_to == 'company':
            # The group works under several registrations (main CR, branches,
            # group companies): the number on the paper says which.
            Registration = self.env['ksw.company.registration']
            reg = Registration._find(who.get('detected_id_number')) or Registration._find(who.get('_number'))
            if reg:
                return reg.company_id, _('Registration %s', reg.name)
            parent = Registration._find(who.get('detected_parent_number'))
            if parent:
                # A branch certificate not registered yet: its main CR says whose.
                return parent.company_id, _('Main CR %s', parent.name)
            return self.env['res.company'], False
        if applies_to == 'vehicle':
            Vehicle = self.env['ksw.fleet.vehicle'].sudo()
            vin = vin_key(who.get('detected_vin'))
            if vin:
                found = Vehicle.search([('x_vin', '!=', False)]).filtered(lambda v: vin_key(v.x_vin) == vin)
                if len(found) == 1:
                    return found, _('Chassis number')
            plate = plate_key(who.get('detected_plate'))
            if plate:
                found = Vehicle.search([('plate_number', '!=', False)]).filtered(
                    lambda v: plate_key(v.plate_number) == plate)
                if len(found) == 1:
                    return found, _('Plate')
            return Vehicle.browse(), False
        Employee = self.env['hr.employee'].sudo()
        number = (who.get('detected_id_number') or '').translate(_DIGITS).strip()
        if number:
            found = Employee.search([('identification_id', '=', number)])
            if len(found) == 1:
                return found, _('Iqama / ID number')
        name = (who.get('detected_name') or '').strip()
        if name:
            found = Employee.search([('name', '=ilike', name)])
            if len(found) == 1:
                return found, _('Name')
        return Employee.browse(), False

    def _identity(self, doc):
        """What ties documents of the same vehicle or person in a batch."""
        if doc.applies_to == 'vehicle':
            return ('vin', vin_key(doc.detected_vin)) if doc.detected_vin else (
                ('plate', plate_key(doc.detected_plate)) if plate_key(doc.detected_plate) else None)
        if doc.applies_to == 'employee':
            number = (doc.detected_id_number or '').strip()
            return ('id', number) if number else None
        return None

    def _group_owners(self, source=None):
        """Documents of the same vehicle (chassis or plate) or person (ID
        number) share an owner: one found, or one HR chose, gives it to the
        others still without one."""
        for batch in self:
            drafts = batch.document_ids.filtered(lambda d: d.state == 'draft')
            owners = {}
            # The record HR just chose wins over the others of its group.
            for doc in (source or self.env['ksw.document']) | drafts:
                key = batch._identity(doc)
                owner = doc.vehicle_id or doc.employee_id
                if key and owner and key not in owners:
                    owners[key] = owner
            # Same vehicle by chassis and plate on different documents: a
            # chassis-identified vehicle's plate ties the plate group to it.
            for doc in drafts.filtered(lambda d: d.applies_to == 'vehicle' and d.detected_vin and d.detected_plate):
                vin, plate = ('vin', vin_key(doc.detected_vin)), ('plate', plate_key(doc.detected_plate))
                if vin in owners and plate not in owners:
                    owners[plate] = owners[vin]
                elif plate in owners and vin not in owners:
                    owners[vin] = owners[plate]
            for doc in drafts.filtered(lambda d: not (d.vehicle_id or d.employee_id or d.company_id)):
                owner = owners.get(batch._identity(doc))
                if owner:
                    if doc.applies_to == 'vehicle':
                        field, note = 'vehicle_id', _('Same vehicle as another document')
                    else:
                        field, note = 'employee_id', _('Same person as another document')
                    doc.with_context(ksw_document_grouping=True).write({field: owner.id, 'match_note': note})

    def action_confirm_all(self):
        """Confirm every draft that says whose it is. Those still missing
        something stay as drafts, each saying what."""
        self._check_officer()
        for batch in self:
            problems = []
            for doc in batch.document_ids.filtered(lambda d: d.state == 'draft'):
                try:
                    with self.env.cr.savepoint():
                        doc.action_confirm()
                except UserError as exc:
                    problems.append('%s: %s' % (doc.name or doc.id, exc.args[0]))
            batch.errors = '\n'.join(problems) or False
            if problems:
                batch.message_post(body=Markup('<br/>').join(problems), subtype_xmlid='mail.mt_note')
        return True
