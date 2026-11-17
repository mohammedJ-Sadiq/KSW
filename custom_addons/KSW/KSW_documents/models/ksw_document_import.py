import base64
import csv
import io
import logging
import re

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Domain

from .hijri import parse_date
from .ksw_document import OFFICER_GROUP
from .plates import plate_key, vin_key

_logger = logging.getLogger(__name__)

# Absher Business ▸ Vehicles export: column header → line field. Headers are
# matched after trimming; the export repeats «وضع المركبة», the first wins.
ABSHER_VEHICLE_COLUMNS = {
    'رقم اللوحة': 'plate', 'الكود': 'code', 'القسم': 'absher_department', 'نوع التسجيل': 'registration_type',
    'الماركة': 'make', 'الطراز': 'model', 'سنة الصنع': 'year', 'الرقم التسلسلي': 'serial',
    'رقم الهيكل': 'vin', 'اللون الأساسي': 'color', 'تاريخ انتهاء رخصة السير': 'istimara_expiry_text',
    'تاريخ انتهاء الفحص': 'mvpi_expiry_text', 'رقم هوية المستخدم الفعلي': 'user_id_number',
    'اسم المستخدم الفعلي': 'user_name', 'حالة الفحص': 'mvpi_status', 'حالة التأمين': 'insurance_status',
    'حالة التحفظ': 'hold_status', 'وضع المركبة': 'vehicle_status', 'نوع الهيكل': 'body_type',
}
# Muqeem ▸ Reports ▸ Active residents.
MUQEEM_COLUMNS = {
    'رقم الاقامة': 'id_number', 'الاسم': 'person_name', 'الجنسية': 'nationality', 'المهنة': 'occupation',
    'رقم الجواز': 'passport_no', 'تاريخ انتهاء الجواز': 'passport_expiry_text',
    'تاريخ انتهاء الاقامة': 'iqama_expiry_text', 'تاريخ الميلاد': 'birth_date_text',
    'خارج المملكه': 'outside_kingdom', 'رقم صاحب العمل': 'employer_number',
}
# GOSI ▸ Active subscribers of one establishment. The wage columns are never
# read: pay belongs to payroll.
GOSI_COLUMNS = {'رقم الهوية': 'id_number', 'اسم المشترك': 'person_name', 'الجنسية': 'nationality',
                'المهنة': 'occupation'}
# The column that identifies each export's header row.
HEADER_MARK = {'absher_vehicles': 'رقم الهيكل', 'muqeem_residents': 'رقم الاقامة', 'gosi_subscribers': 'رقم الهوية'}
# Imported every month (a reminder on Home); GOSI only when HR wants to check.
MONTHLY_SOURCES = ('absher_vehicles', 'muqeem_residents')
VALID = 'ساري المفعول'
NONE_VALUES = ('', '-', 'لا يوجد')


def _arabic_name_key(text):
    """A company name compared loosely: no «شركة», no spaces, ة = ه."""
    return re.sub(r'\s+', '', (text or '').replace('شركة', '').replace('ة', 'ه'))


def fleet_key(text):
    """The fleet number however it is written: "T099", "T99", "t 99" → "T99";
    "س 43" and "43 س" → "س43". Names that are not a code give None."""
    text = re.sub(r'\s+', ' ', (text or '').strip().upper())
    match = re.fullmatch(r'([^\d\s]+) ?0*(\d+)', text) or re.fullmatch(r'0*(\d+) ?([^\d\s]+)', text)
    if not match:
        return None
    letters, digits = match.groups() if not match.group(1).isdigit() else match.groups()[::-1]
    return '%s%s' % (letters, int(digits))


class KswDocumentImport(models.Model):
    """The monthly reconciliation with a government portal's export. Each
    month the Documents Officer downloads the export and imports it here:
    new vehicles are added, renewed documents updated, and what no longer
    appears in the export is put in front of HR, never archived silently.
    The export is the authority for numbers and dates; scans are only proof."""
    _name = 'ksw.document.import'
    _description = 'Government Export Import'
    _inherit = ['mail.thread']
    _order = 'id desc'

    name = fields.Char(compute='_compute_name', store=True, readonly=False, precompute=True, required=True)
    source = fields.Selection([('absher_vehicles', 'Absher Business: Vehicles'),
                               ('muqeem_residents', 'Muqeem: Active Residents'),
                               ('gosi_subscribers', 'GOSI: Active Subscribers')], required=True,
                              default='absher_vehicles')
    registration_id = fields.Many2one('ksw.company.registration', string='Establishment (CR)',
                                      help='GOSI lists one establishment per file: the registration its '
                                           'subscribers are under. Found from the file header.')
    file = fields.Binary(string='Export File', attachment=True)
    file_name = fields.Char()
    state = fields.Selection([('draft', 'To Import'), ('preview', 'To Apply'), ('done', 'Applied')],
                             default='draft', required=True, tracking=True)
    line_ids = fields.One2many('ksw.document.import.line', 'import_id', string='Rows')
    applied_by_id = fields.Many2one('res.users', readonly=True)
    applied_date = fields.Datetime(readonly=True)
    summary = fields.Html(readonly=True)
    row_count = fields.Integer(compute='_compute_counts')
    matched_count = fields.Integer(compute='_compute_counts')
    create_count = fields.Integer(compute='_compute_counts')
    warning_count = fields.Integer(compute='_compute_counts')
    missing_count = fields.Integer(compute='_compute_counts')
    x_is_pending_my_action = fields.Boolean(compute='_compute_is_pending_my_action',
                                            search='_search_is_pending_my_action', string='Waiting for Me')

    @api.depends('source')
    def _compute_name(self):
        month = fields.Date.context_today(self).strftime('%Y-%m')
        labels = dict(self._fields['source']._description_selection(self.env))
        for rec in self:
            if not rec.name or rec.name.startswith(tuple(labels.values())):
                rec.name = '%s %s' % (labels.get(rec.source, ''), month)

    @api.depends('line_ids.action', 'line_ids.vehicle_id', 'line_ids.employee_id', 'line_ids.warnings',
                 'line_ids.kind')
    def _compute_counts(self):
        for rec in self:
            rows = rec.line_ids.filtered(lambda l: l.kind == 'row')
            rec.row_count = len(rows)
            rec.matched_count = len(rows.filtered(lambda l: l.vehicle_id or l.employee_id))
            rec.create_count = len(rows.filtered(lambda l: l.action == 'create'))
            rec.warning_count = len(rec.line_ids.filtered('warnings'))
            rec.missing_count = len(rec.line_ids.filtered(lambda l: l.kind == 'missing'))

    # -- Home: the officer's monthly step ------------------------------------
    @api.model
    def _pending_domain(self):
        if not (self.env.su or self.env.user.has_group(OFFICER_GROUP)):
            return Domain([('id', '=', False)])
        return Domain([('state', 'in', ('draft', 'preview'))])

    @api.depends_context('uid')
    def _compute_is_pending_my_action(self):
        mine = set(self.search(self._pending_domain()).ids)
        for rec in self:
            rec.x_is_pending_my_action = rec.id in mine

    def _search_is_pending_my_action(self, operator, value):
        if operator in ('in', 'not in'):
            wanted = (operator == 'in') == any(value)
        elif operator in ('=', '!='):
            wanted = (operator == '=') == bool(value)
        else:
            return NotImplemented
        domain = self._pending_domain()
        return domain if wanted else ~domain

    @api.model
    def _cron_monthly_imports(self):
        """On the 1st: this month's import waits on Home until it is done."""
        month = fields.Date.context_today(self).strftime('%Y-%m')
        first = fields.Date.context_today(self).replace(day=1)
        labels = dict(self._fields['source'].selection)
        for source in MONTHLY_SOURCES:
            if not self.search_count([('source', '=', source), ('create_date', '>=', first)]):
                self.create({'source': source, 'name': '%s %s' % (labels[source], month)})

    def _check_officer(self):
        if not (self.env.su or self.env.user.has_group(OFFICER_GROUP)):
            raise UserError(_('Only HR imports government exports.'))

    # -- reading the file ----------------------------------------------------
    def _read_rows(self):
        """The export as a list of {header: text}, from .xlsx or .csv."""
        self.ensure_one()
        if not self.file:
            raise UserError(_('Attach the export file first.'))
        raw = base64.b64decode(self.file)
        name = (self.file_name or '').lower()
        if name.endswith(('.xlsx', '.xlsm')) or raw[:2] == b'PK':
            import openpyxl
            sheet = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True).active
            table = [[('' if c is None else c) for c in row] for row in sheet.iter_rows(values_only=True)]
        else:
            text = raw.decode('utf-8-sig', errors='replace')
            try:
                delimiter = csv.Sniffer().sniff(text[:4096], delimiters=',;\t').delimiter
            except csv.Error:
                # Short or ragged files defeat the sniffer: the commonest separator wins.
                delimiter = max('\t;,', key=text[:4096].count)
            table = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        mark = HEADER_MARK[self.source]
        for i, row in enumerate(table):
            cells = [str(c).strip() for c in row]
            if mark in cells:
                headers = cells
                break
        else:
            raise UserError(_('This is not the %(s)s export: no «%(c)s» column found.',
                              s=dict(self._fields['source']._description_selection(self.env))[self.source], c=mark))
        # What a GOSI list prints above its table: the establishment.
        preamble = [str(c).strip() for row in table[:i] for c in row if str(c).strip()]
        rows = []
        for row in table[i + 1:]:
            values = {}
            for header, cell in zip(headers, row):
                if header and header not in values:
                    values[header] = cell.strip() if isinstance(cell, str) else cell
            if any(v not in NONE_VALUES for v in values.values() if isinstance(v, str)):
                rows.append(values)
        return rows, preamble

    # -- preview -------------------------------------------------------------
    def action_preview(self):
        self._check_officer()
        for rec in self:
            rec.line_ids.unlink()
            rows, preamble = rec._read_rows()
            getattr(rec, '_preview_' + rec.source)(rows, preamble)
            rec.state = 'preview'
        return True

    def _preview_absher_vehicles(self, rows, preamble):
        rec = self
        Line = self.env['ksw.document.import.line']
        Vehicle = self.env['ksw.fleet.vehicle'].sudo()
        vehicles = Vehicle.search([])
        by_vin = {vin_key(v.x_vin): v for v in vehicles if v.x_vin}
        by_plate = {}
        by_code = {}
        by_digits = {}
        for v in vehicles:
            if plate_key(v.plate_number):
                by_plate.setdefault(plate_key(v.plate_number), v)
            # A plate kept as digits only ("3463"): matched on its digits, or
            # the import would add the same vehicle a second time.
            if (v.plate_number or '').strip().isdigit():
                by_digits.setdefault(v.plate_number.strip(), Vehicle.browse())
                by_digits[v.plate_number.strip()] |= v
            if fleet_key(v.name):
                by_code.setdefault(fleet_key(v.name), Vehicle.browse())
                by_code[fleet_key(v.name)] |= v
        codes = [fleet_key(r.get('الكود')) for r in rows]
        file_digits = [re.sub(r'\D', '', str(r.get('رقم اللوحة') or '')) for r in rows]
        vals_list = []
        for n, row in enumerate(rows, 1):
            vals = {'import_id': rec.id, 'row_no': n, 'kind': 'row'}
            for header, field in ABSHER_VEHICLE_COLUMNS.items():
                value = row.get(header, '')
                vals[field] = '' if value is None else str(value).strip()
            key = codes[n - 1]
            vehicle, how = Vehicle.browse(), False
            if vin_key(vals['vin']) in by_vin:
                vehicle, how = by_vin[vin_key(vals['vin'])], _('Chassis No.')
            elif plate_key(vals['plate']) in by_plate:
                vehicle, how = by_plate[plate_key(vals['plate'])], _('Plate')
            elif len(by_digits.get(re.sub(r'\D', '', vals['plate']), [])) == 1 \
                    and file_digits.count(re.sub(r'\D', '', vals['plate'])) == 1:
                vehicle, how = by_digits[re.sub(r'\D', '', vals['plate'])], _('Plate digits')
            elif key and codes.count(key) == 1 and len(by_code.get(key, [])) == 1:
                vehicle, how = by_code[key], _('Fleet No.')
            vals.update(vehicle_id=vehicle.id, match_note=how,
                        action='update' if vehicle else 'create')
            vals_list.append(vals)
        lines = Line.create(vals_list)
        lines._compute_warnings()
        # Vehicles once confirmed by Absher (they carry a chassis no.) that
        # are no longer in it: sold, scrapped, transferred, or a mistake.
        seen = lines.vehicle_id
        gone = vehicles.filtered(lambda v: v.x_vin and v not in seen)
        Line.create([{'import_id': rec.id, 'kind': 'missing', 'vehicle_id': v.id, 'action': 'skip',
                      'plate': v.plate_number, 'vin': v.x_vin, 'code': v.name,
                      'warnings': _('No longer in the Absher export: sold, scrapped or transferred? '
                                    'Archive it in Fleet if so.')} for v in gone])

    def _employees_by_id_number(self):
        """Employees by iqama / Saudi ID: SSN No. is where payroll keeps it."""
        found = {}
        for emp in self.env['hr.employee'].sudo().with_context(active_test=False).search(
                ['|', ('ssnid', '!=', False), ('identification_id', '!=', False)]):
            for number in {(emp.ssnid or '').strip(), (emp.identification_id or '').strip()} - {''}:
                found.setdefault(number, emp)
        return found

    def _people_lines(self, rows, columns):
        employees = self._employees_by_id_number()
        vals_list = []
        for n, row in enumerate(rows, 1):
            vals = {'import_id': self.id, 'row_no': n, 'kind': 'row'}
            for header, field in columns.items():
                value = row.get(header, '')
                vals[field] = '' if value is None else str(value).strip()
            if not vals['id_number']:
                continue
            emp = employees.get(vals['id_number'])
            vals.update(employee_id=emp and emp.id, match_note=emp and _('Iqama / ID number'),
                        action='update' if emp else 'skip')
            vals_list.append(vals)
        lines = self.env['ksw.document.import.line'].create(vals_list)
        lines._compute_warnings()
        return lines

    def _preview_muqeem_residents(self, rows, preamble):
        lines = self._people_lines(rows, MUQEEM_COLUMNS)
        # Active employees on an iqama that Muqeem no longer lists.
        listed = set(lines.mapped('id_number'))
        gone = self.env['hr.employee'].sudo().search([('ssnid', '=like', '2%')]).filtered(
            lambda e: e.ssnid.strip() not in listed)
        self.env['ksw.document.import.line'].create([{
            'import_id': self.id, 'kind': 'missing', 'employee_id': e.id, 'action': 'skip',
            'id_number': e.ssnid, 'person_name': e.name,
            'warnings': _('Active in Odoo but not in Muqeem: final exit, transferred, or a wrong iqama no. '
                          'in Odoo?')} for e in gone])

    def _preview_gosi_subscribers(self, rows, preamble):
        if not self.registration_id:
            names = [_arabic_name_key(t) for t in preamble if 'المنشأة' not in t]
            for reg in self.env['ksw.company.registration'].sudo().search([]):
                key = _arabic_name_key(reg.name.split('(')[0])
                if any(n and (n in key or key in n) for n in names):
                    self.registration_id = reg
                    break
        self._people_lines(rows, GOSI_COLUMNS)

    # -- apply ---------------------------------------------------------------
    def action_apply(self):
        self._check_officer()
        for rec in self:
            if rec.state != 'preview':
                raise UserError(_('Preview the export first.'))
            tally = {}
            for line in rec.line_ids.filtered(lambda l: l.kind == 'row' and l.action != 'skip'):
                for what in line._apply():
                    tally[what] = tally.get(what, 0) + 1
            rec.write({'state': 'done', 'applied_by_id': self.env.user.id,
                       'applied_date': fields.Datetime.now(),
                       'summary': Markup('<br/>').join(
                           Markup('%(k)s: <b>%(n)s</b>') % {'k': k, 'n': n} for k, n in sorted(tally.items()))})
            rec.message_post(body=rec.summary, subtype_xmlid='mail.mt_note')
        return True


class KswDocumentImportLine(models.Model):
    _name = 'ksw.document.import.line'
    _description = 'Government Export Row'
    _order = 'kind desc, row_no'

    import_id = fields.Many2one('ksw.document.import', required=True, ondelete='cascade', index=True)
    kind = fields.Selection([('row', 'In the Export'), ('missing', 'Not in the Export')], required=True,
                            default='row')
    row_no = fields.Integer(string='#')
    action = fields.Selection([('update', 'Update'), ('create', 'Create Vehicle'), ('skip', 'Skip')],
                              required=True, default='update')
    vehicle_id = fields.Many2one('ksw.fleet.vehicle', string='Vehicle in Odoo')
    match_note = fields.Char(string='Found By', readonly=True)
    warnings = fields.Text(readonly=True)
    result = fields.Char(readonly=True)
    plate = fields.Char()
    code = fields.Char(string='Code (Absher)')
    absher_department = fields.Char(string='Department (Absher)')
    registration_type = fields.Char()
    make = fields.Char()
    model = fields.Char()
    year = fields.Char()
    serial = fields.Char(string='Istimara Serial')
    vin = fields.Char(string='Chassis No.')
    color = fields.Char()
    istimara_expiry_text = fields.Char(string='Istimara Expiry (Hijri)')
    mvpi_expiry_text = fields.Char(string='Inspection Expiry (Hijri)')
    istimara_expiry = fields.Date(compute='_compute_dates')
    mvpi_expiry = fields.Date(compute='_compute_dates')
    mvpi_status = fields.Char(string='Inspection')
    insurance_status = fields.Char(string='Insurance')
    hold_status = fields.Char(string='Hold')
    vehicle_status = fields.Char()
    body_type = fields.Char()
    user_id_number = fields.Char(string='Actual User ID')
    user_name = fields.Char(string='Actual User')
    user_employee_id = fields.Many2one('hr.employee', compute='_compute_user_employee', string='Actual User in Odoo')
    # People (Muqeem, GOSI)
    employee_id = fields.Many2one('hr.employee', string='Employee in Odoo')
    id_number = fields.Char(string='Iqama / ID No.')
    person_name = fields.Char(string='Name')
    nationality = fields.Char()
    occupation = fields.Char()
    passport_no = fields.Char(string='Passport No.')
    passport_expiry_text = fields.Char(string='Passport Expiry (as exported)')
    iqama_expiry_text = fields.Char(string='Iqama Expiry (as exported)')
    birth_date_text = fields.Char(string='Birth Date (as exported)')
    outside_kingdom = fields.Char(string='Outside the Kingdom')
    employer_number = fields.Char(string='Employer No.')
    passport_expiry = fields.Date(compute='_compute_dates')
    iqama_expiry = fields.Date(compute='_compute_dates')
    birth_date = fields.Date(compute='_compute_dates')
    line_registration_id = fields.Many2one('ksw.company.registration', compute='_compute_line_registration',
                                           string='Registered Under')

    @api.depends('istimara_expiry_text', 'mvpi_expiry_text', 'passport_expiry_text', 'iqama_expiry_text',
                 'birth_date_text')
    def _compute_dates(self):
        for line in self:
            for src, dst in (('istimara_expiry_text', 'istimara_expiry'), ('mvpi_expiry_text', 'mvpi_expiry'),
                             ('passport_expiry_text', 'passport_expiry'), ('iqama_expiry_text', 'iqama_expiry'),
                             ('birth_date_text', 'birth_date')):
                try:
                    line[dst] = parse_date(line[src])
                except ValueError:
                    line[dst] = False

    @api.depends('user_id_number')
    def _compute_user_employee(self):
        Employee = self.env['hr.employee'].sudo().with_context(active_test=False)
        for line in self:
            number = (line.user_id_number or '').strip()
            line.user_employee_id = number not in NONE_VALUES and Employee.search(
                ['|', ('ssnid', '=', number), ('identification_id', '=', number)], limit=1)

    @api.depends('employer_number', 'import_id.registration_id')
    def _compute_line_registration(self):
        Registration = self.env['ksw.company.registration']
        for line in self:
            line.line_registration_id = (Registration._find(line.employer_number) if line.employer_number
                                         else line.import_id.registration_id)

    def _compute_warnings(self):
        """What HR should look at; none of it is written over Odoo's data."""
        people = self.filtered(lambda l: l.import_id.source != 'absher_vehicles')
        people._compute_people_warnings()
        for line in (self - people).filtered(lambda l: l.kind == 'row'):
            notes = []
            v = line.vehicle_id.sudo()
            digits_only = (v.plate_number or '').strip().isdigit() \
                and v.plate_number.strip() == re.sub(r'\D', '', line.plate or '')
            if v.plate_number and not digits_only and plate_key(v.plate_number) != plate_key(line.plate):
                notes.append(_('Plate in Odoo %(o)s ≠ Absher %(a)s', o=v.plate_number, a=line.plate))
            if v.x_vin and vin_key(v.x_vin) != vin_key(line.vin):
                notes.append(_('Chassis in Odoo %(o)s ≠ Absher %(a)s', o=v.x_vin, a=line.vin))
            custodian = 'x_custodian_id' in v._fields and v.x_custodian_id
            if line.user_employee_id and custodian and custodian != line.user_employee_id:
                notes.append(_('Absher actual user %(a)s, custodian in Odoo %(o)s: update Absher after the '
                               'handover (HR-12), or check the custody.', a=line.user_name, o=custodian.name))
            elif line.user_id_number not in NONE_VALUES and not line.user_employee_id:
                notes.append(_('Actual user %s is not an employee in Odoo.', line.user_name))
            for label, text, value in ((_('Istimara expiry'), line.istimara_expiry_text, line.istimara_expiry),
                                       (_('Inspection expiry'), line.mvpi_expiry_text, line.mvpi_expiry)):
                if text and text not in NONE_VALUES and not value:
                    notes.append(_('%(l)s not a date: %(t)s', l=label, t=text))
            if line.insurance_status != VALID:
                notes.append(_('No valid insurance on Absher.'))
            if line.hold_status and line.hold_status not in ('غير متحفظ',) + NONE_VALUES:
                notes.append(_('Absher hold: %s', line.hold_status))
            line.warnings = '\n'.join(notes) or False

    def _compute_people_warnings(self):
        for line in self.filtered(lambda l: l.kind == 'row'):
            notes = []
            emp = line.employee_id.sudo()
            if not emp:
                notes.append(_('Not an employee in Odoo: add the employee, then preview again.'))
            if line.outside_kingdom == 'نعم':
                notes.append(_('Outside the Kingdom now (Muqeem).'))
            if emp and emp.x_registration_id and line.line_registration_id \
                    and emp.x_registration_id != line.line_registration_id:
                notes.append(_('Registered under %(o)s in Odoo, %(g)s here: check which is right.',
                               o=emp.x_registration_id.name, g=line.line_registration_id.name))
            if line.employer_number and not line.line_registration_id:
                notes.append(_('Employer no. %s is not one of the registrations.', line.employer_number))
            for label, text, value in ((_('Iqama expiry'), line.iqama_expiry_text, line.iqama_expiry),
                                       (_('Passport expiry'), line.passport_expiry_text, line.passport_expiry)):
                if text and text not in NONE_VALUES and not value:
                    notes.append(_('%(l)s not a date: %(t)s', l=label, t=text))
            line.warnings = '\n'.join(notes) or False

    def _vehicle_type(self):
        model = (self.model or '') + ' ' + (self.body_type or '')
        if 'رأس' in model or 'راس' in model:
            return 'trailer'
        if 'ايسوزو' in (self.make or '') and ('صهريج' in model or 'REWARD' in model.upper()):
            return 'isuzu'
        return 'other'

    def _apply(self):
        """Write one row. Returns what happened, for the summary."""
        self.ensure_one()
        if self.import_id.source == 'absher_vehicles':
            return self._apply_vehicle()
        return self._apply_person()

    def _apply_person(self):
        """Muqeem / GOSI: the registration the employee is under, and (Muqeem)
        the iqama and passport, verified. Fills what is empty, never overwrites."""
        emp = self.employee_id.sudo()
        if not emp:
            return []
        done = []
        fill = {}
        if self.line_registration_id and not emp.x_registration_id:
            fill['x_registration_id'] = self.line_registration_id.id
        if self.nationality and not emp.country_id:
            country = self.env['ksw.document']._find_country(self.nationality)
            if country:
                fill['country_id'] = country.id
        if self.birth_date and not emp.birthday:
            fill['birthday'] = self.birth_date
        if fill:
            emp.write(fill)
            done.append(_('Employee details filled'))
        if self.import_id.source == 'muqeem_residents':
            types = {t.code: t for t in self.env['ksw.document.type'].search([])}
            Document = self.env['ksw.document']
            labels = {'new': _('Documents recorded'), 'renewed': _('Documents renewed'),
                      'same': _('Documents unchanged'), 'older': _('Older than the register (ignored)')}
            if 'iqama' in types:
                done.append(labels[Document._record_from_source(
                    types['iqama'], emp, 'muqeem', self.import_id, number=self.id_number,
                    expiry=self.iqama_expiry or False)])
            if 'passport' in types and self.passport_no:
                done.append(labels[Document._record_from_source(
                    types['passport'], emp, 'muqeem', self.import_id, number=self.passport_no,
                    expiry=self.passport_expiry or False)])
        self.result = ', '.join(sorted(set(done)))
        return done

    def _apply_vehicle(self):
        done = []
        Vehicle = self.env['ksw.fleet.vehicle'].sudo()
        vehicle = self.vehicle_id.sudo()
        if self.action == 'create' and not vehicle:
            key = fleet_key(self.code)
            name = self.code if key and not Vehicle.search_count([('name', '=', self.code)]) else self.plate
            vehicle = Vehicle.create({'name': name, 'vehicle_type': self._vehicle_type()})
            self.vehicle_id = vehicle
            done.append(_('Vehicles created'))
        # Absher is the registry: it fills what Odoo lacks, never overwrites.
        master = {'plate_number': self.plate, 'x_vin': self.vin, 'brand': self.make, 'model': self.model,
                  'x_color': self.color}
        if self.year and self.year.isdigit():
            master['year'] = int(self.year)
        fill = {f: v for f, v in master.items() if v not in NONE_VALUES and f in vehicle._fields and not vehicle[f]}
        # "3463" typed for «أ ع ط 3463» is completed, not overwritten.
        if (vehicle.plate_number or '').strip().isdigit() \
                and vehicle.plate_number.strip() == re.sub(r'\D', '', self.plate or ''):
            fill['plate_number'] = self.plate
        if fill:
            vehicle.write(fill)
            done.append(_('Vehicle details filled'))
        types = {t.code: t for t in self.env['ksw.document.type'].search([])}
        Document = self.env['ksw.document']
        imp = self.import_id
        outcomes = []
        if 'istimara' in types:
            outcomes.append(Document._record_from_source(
                types['istimara'], vehicle, 'absher', imp, number=self.serial or False,
                expiry=self.istimara_expiry or False, verified=True))
        if 'mvpi' in types:
            if self.mvpi_expiry:
                outcomes.append(Document._record_from_source(
                    types['mvpi'], vehicle, 'absher', imp, expiry=self.mvpi_expiry))
            elif self.mvpi_status in NONE_VALUES:
                # A new vehicle is not inspected yet: nothing is due.
                outcomes.append(Document._record_from_source(
                    types['mvpi'], vehicle, 'absher', imp, verified=True,
                    note=_('Not required yet (new vehicle), per Absher.')))
        if 'insurance' in types and self.insurance_status == VALID:
            outcomes.append(Document._record_from_source(types['insurance'], vehicle, 'absher', imp,
                                                         verified=True))
        labels = {'new': _('Documents recorded'), 'renewed': _('Documents renewed'),
                  'same': _('Documents unchanged'), 'older': _('Older than the register (ignored)')}
        done += [labels[o] for o in outcomes]
        self.result = ', '.join(sorted(set(done)))
        return done
