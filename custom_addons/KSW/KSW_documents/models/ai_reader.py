"""Read scanned documents with Claude into draft fields.

Uses KSW_ai_assistant's Claude client and its Settings (provider, key,
model). Never trusted on its own: each result is a draft HR confirms, with
what the reader was unsure of listed for HR to check.
"""
import base64
import json
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from odoo.addons.KSW_ai_assistant.models import claude_client

_logger = logging.getLogger(__name__)

_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}

_SYSTEM = """You read Saudi official documents from a scan, a photo (possibly rotated or at an angle)
or a phone screenshot. ONE image may contain SEVERAL documents (e.g. an Absher screenshot with
both the Resident ID and the Driving License): return each one.
Known kinds: vehicle registration «رخصة سير» (its card has NO expiry date: leave it null), insurance,
periodic inspection «الفحص الدوري», operation card «بطاقة تشغيل» (Transport General Authority / Naql;
take the "Card" dates, not the company "License" dates), driving licence «رخصة سياقة/قيادة»,
driver card «بطاقة سائق», Resident ID «هوية مقيم / إقامة», passport, visa, and the company transport
licence (the "License Info" block of an operation card) as type company_transport_licence, and a
commercial registration certificate «شهادة السجل التجاري / شهادة تسجيل فرع» as type
commercial_registration (one document per certificate, however many pages: number = the CR No.
«رقم السجل التجاري» if printed, else the unified number; id_number = the unified number «الرقم
الوطني الموحد / الرقم الموحد للمنشأة»; holder_name = the trade name «الاسم التجاري»;
parent_cr_number = «رقم سجل المركز الرئيسي» on a branch certificate).
Dates: Absher shows DD/MM/YYYY; Naql shows YYYY-MM-DD; older cards are Hijri (Umm al-Qura): always
return Gregorian YYYY-MM-DD and also the date exactly as printed.
Answer with ONE JSON object and nothing else:
{"documents": [{
  "type_code": one of the given codes or null,
  "number": the document's own number (card / licence / policy / registration serial; for a
            Resident ID or a driving licence: the ID number) or null,
  "issue_date": "YYYY-MM-DD" or null, "expiry_date": "YYYY-MM-DD" or null,
  "expiry_date_as_printed": as printed or null,
  "license_class": e.g. "Heavy Transport / نقل ثقيل" or null,
  "holder_name": the person's name (Latin if printed, else Arabic) or null,
  "id_number": the person's iqama / national ID number; on a company document the company's
               unified / registry number (starts with 7); else null,
  "nationality": in English or null,
  "parent_cr_number": the main CR number on a branch certificate, or null,
  "plate": the plate ONCE, in one script, e.g. "8935 RXA" (Latin if printed, else Arabic), or null,
  "vin": the chassis number «رقم الهيكل» or null,
  "make": e.g. "Mercedes", "model": e.g. "Trailer head" / "Reward", "year": 2020, "color": in English,
  "confidence": "high" | "medium" | "low",
  "doubts": [short notes on anything unclear]}]}
Never guess a number or a date you cannot read: use null and say so in doubts. Different company
names on owner and employer are normal (the group has several companies): do not flag them."""


class KswDocumentAiReader(models.AbstractModel):
    _name = 'ksw.document.ai.reader'
    _description = 'Document AI Reader'

    @api.model
    def _settings(self):
        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ksw_ai.provider') != 'anthropic' or not params.get_param('ksw_ai.api_key'):
            return None
        return {'key': params.get_param('ksw_ai.api_key'), 'model': params.get_param('ksw_ai.model') or None}

    @api.model
    def _is_available(self):
        return bool(self._settings())

    @api.model
    def _ask(self, attachment, types):
        settings = self._settings()
        if not settings:
            raise UserError(_('Reading documents needs Claude: set Settings ▸ AI Provider to Anthropic '
                              'Claude and add its API key.'))
        mimetype = attachment.mimetype or ''
        data = base64.b64encode(attachment.raw or b'').decode()
        if mimetype in _IMAGE_TYPES:
            block = {'type': 'image', 'source': {'type': 'base64', 'media_type': mimetype, 'data': data}}
        elif mimetype == 'application/pdf':
            block = {'type': 'document', 'source': {'type': 'base64', 'media_type': mimetype, 'data': data}}
        else:
            raise UserError(_('%s is not a photo or a PDF.', attachment.name))
        choices = '\n'.join(f'- {t.code}: {t.name} ({t.ai_hint or ""})' for t in types)
        messages = [{'role': 'user', 'content': [
            block, {'type': 'text', 'text': f'Possible document types (code: name):\n{choices}'}]}]
        try:
            response = claude_client.create_message(settings['key'], settings['model'], _SYSTEM, messages,
                                                    tools=[], effort='medium', max_tokens=4000)
        except claude_client.ClaudeError as exc:
            raise UserError(_('The AI reader could not be reached (%s). Fill the document by hand.', exc)) from exc
        return ''.join(b.get('text', '') for b in response.get('content', []) if b.get('type') == 'text')

    @api.model
    def _read_all(self, attachment, types):
        """Every document in `attachment`: a list of draft values, each with
        its 'notes' and what it says about its owner ('_who')."""
        return self._parse_all(self._ask(attachment, types), types)

    @api.model
    def _read(self, attachment, types):
        """The first document only (single upload for a known owner)."""
        found = self._read_all(attachment, types)
        return found[0] if found else {'notes': [_('The AI reader found no document: fill it by hand.')],
                                       '_who': {}}

    @api.model
    def _parse_all(self, text, types):
        match = re.search(r'\{.*\}', text or '', re.S)
        try:
            data = json.loads(match.group(0)) if match else {}
        except ValueError:
            data = {}
        docs = data.get('documents')
        if docs is None and data:
            docs = [data]  # a single object answer
        return [self._parse_one(d, types) for d in (docs or [])]

    @api.model
    def _parse(self, text, types):
        found = self._parse_all(text, types)
        return found[0] if found else {
            'notes': [_('The AI reader gave no usable answer: fill the document by hand.')], '_who': {},
            '_holder': None, '_plate': None}

    @api.model
    def _parse_one(self, data, types):
        notes = list(data.get('doubts') or [])
        vals = {}
        doc_type = types.filtered(lambda t: t.code == data.get('type_code'))[:1]
        if doc_type:
            vals['type_id'] = doc_type.id
        else:
            notes.append(_('Document type not recognised: choose it.'))
        for key in ('number', 'license_class'):
            if data.get(key):
                vals[key] = str(data[key]).strip()
        for key in ('issue_date', 'expiry_date'):
            value = data.get(key)
            try:
                if value:
                    vals[key] = fields.Date.to_date(value)
            except ValueError:
                notes.append(_('%(f)s could not be read as a date: %(v)s', f=key, v=value))
        if data.get('expiry_date_as_printed') and data.get('expiry_date_as_printed') != data.get('expiry_date'):
            notes.append(_('Expiry as printed: %s (converted to Gregorian; check it).',
                           data['expiry_date_as_printed']))
        if data.get('confidence') in ('low', 'medium'):
            notes.append(_('The reader was only %s confident: check every field.', data['confidence']))
        who = {
            'detected_plate': data.get('plate'), 'detected_vin': data.get('vin'),
            'detected_id_number': data.get('id_number'), 'detected_name': data.get('holder_name'),
            'detected_nationality': data.get('nationality'), 'detected_make': data.get('make'),
            'detected_model': data.get('model'), 'detected_color': data.get('color'),
            'detected_parent_number': data.get('parent_cr_number'),
        }
        try:
            who['detected_year'] = int(data.get('year') or 0)
        except (TypeError, ValueError):
            who['detected_year'] = 0
        vals['_who'] = {k: (str(v).strip() if isinstance(v, str) else v) for k, v in who.items() if v}
        vals['_holder'] = data.get('holder_name')
        vals['_plate'] = data.get('plate')
        vals['notes'] = notes
        return vals
