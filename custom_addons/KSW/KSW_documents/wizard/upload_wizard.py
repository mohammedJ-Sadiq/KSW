import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.ksw_document import OFFICER_GROUP


def _norm(text):
    """Letters and digits only, for comparing a plate or a name loosely."""
    return re.sub(r'[\W_]+', '', (text or '').lower())


class KswDocumentUploadWizard(models.TransientModel):
    """Upload scans for one vehicle or employee: each becomes a draft
    document, read by Claude when it is set up, for HR to confirm."""
    _name = 'ksw.document.upload.wizard'
    _description = 'Upload Documents'

    applies_to = fields.Selection([('vehicle', 'Vehicle'), ('employee', 'Employee')], required=True)
    vehicle_id = fields.Many2one('ksw.fleet.vehicle', readonly=True)
    employee_id = fields.Many2one('hr.employee', readonly=True)
    attachment_ids = fields.Many2many('ir.attachment', 'ksw_document_upload_attachment_rel', 'wizard_id',
                                      'attachment_id', string='Scans or Photos', required=True)
    type_id = fields.Many2one('ksw.document.type', string='Document',
                              domain="[('applies_to', '=', applies_to)]",
                              help='Leave empty for the AI reader to tell; required without it.')
    use_ai = fields.Boolean(string='Read with AI', default=lambda self: self._default_use_ai())
    ai_available = fields.Boolean(default=lambda self: self._default_use_ai(), readonly=True)

    @api.model
    def _default_use_ai(self):
        return self.env['ksw.document.ai.reader']._is_available()

    @api.model
    def _open_for(self, vehicle=None, employee=None):
        return {
            'type': 'ir.actions.act_window', 'name': _('Upload Documents'),
            'res_model': self._name, 'view_mode': 'form', 'target': 'new',
            'context': {'default_applies_to': 'vehicle' if vehicle else 'employee',
                        'default_vehicle_id': vehicle.id if vehicle else False,
                        'default_employee_id': employee.id if employee else False},
        }

    def action_upload(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group(OFFICER_GROUP)):
            raise UserError(_('Only HR uploads documents.'))
        if not self.attachment_ids:
            raise UserError(_('Add at least one scan or photo.'))
        if not self.use_ai and not self.type_id:
            raise UserError(_('Choose the document type (or read it with AI).'))
        types = self.type_id or self.env['ksw.document.type'].search([('applies_to', '=', self.applies_to)])
        Reader = self.env['ksw.document.ai.reader']
        docs = self.env['ksw.document']
        for attachment in self.attachment_ids.sudo():
            vals, notes = {}, []
            if self.use_ai:
                read = Reader._read(attachment, types)
                notes = read.pop('notes')
                holder, plate = read.pop('_holder'), read.pop('_plate')
                vals.update(read.pop('_who', {}))
                vals.update(read)
                notes += self._mismatches(holder, plate)
            vals.setdefault('type_id', self.type_id.id or False)
            if not vals.get('type_id'):
                # The reader could not tell: keep the scan, HR picks the type.
                vals['type_id'] = types[:1].id
                notes.append(_('Type guessed as the first possible one: check it.'))
            docs |= docs.sudo().create(dict(
                vals, applies_to=self.applies_to, vehicle_id=self.vehicle_id.id,
                employee_id=self.employee_id.id, attachment_ids=[(6, 0, attachment.ids)],
                ai_read=self.use_ai, ai_notes='\n'.join(f'• {n}' for n in notes) or False))
        action = self.env['ir.actions.act_window']._for_xml_id('KSW_documents.action_ksw_document')
        action['context'] = {}
        if len(docs) == 1:
            action.update({'res_id': docs.id, 'view_mode': 'form', 'views': [(False, 'form')]})
        else:
            action['domain'] = [('id', 'in', docs.ids)]
        return action

    def _mismatches(self, holder, plate):
        notes = []
        if self.vehicle_id and plate and self.vehicle_id.plate_number \
                and _norm(plate) not in _norm(self.vehicle_id.plate_number) \
                and _norm(self.vehicle_id.plate_number) not in _norm(plate):
            notes.append(_('Plate on the document (%(d)s) differs from the vehicle\'s (%(v)s).',
                           d=plate, v=self.vehicle_id.plate_number))
        if self.employee_id and holder:
            words = {w for w in re.split(r'\s+', holder.lower()) if len(w) > 2}
            name = self.employee_id.name.lower()
            if words and not any(w in name for w in words):
                notes.append(_('Name on the document (%(d)s) does not look like %(e)s.',
                               d=holder, e=self.employee_id.name))
        return notes
