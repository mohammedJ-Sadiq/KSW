import base64
import io
from datetime import date

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

HEADER = ('رقم الاقامة', 'الاسم', 'نوع رخصة القيادة', 'حالة رخصة القيادة', 'تاريخ الانتهاء (ميلادي)')


@tagged('post_install', '-at_install')
class TestLicenceImport(TransactionCase):
    """Absher ▸ Driving licence holders (Nov 2026 export layout): the
    register gets each driver's licence, and an open request for it closes."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.officer = cls.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'LIC Officer', 'login': 'lic_officer', 'group_ids': [(6, 0, [
                cls.env.ref('KSW_documents.group_document_officer').id, cls.env.ref('hr.group_hr_user').id])]})
        cls.supervisor = cls.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'LIC Supervisor', 'login': 'lic_supervisor',
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id])]})
        Employee = cls.env['hr.employee']
        cls.heavy = Employee.create({'name': 'LIC Heavy', 'ssnid': '2999997701'})
        cls.private = Employee.create({'name': 'LIC Private', 'ssnid': '2999997702'})
        cls.unlicensed = Employee.create({'name': 'LIC None', 'ssnid': '2999997703'})
        Vehicle = cls.env['ksw.fleet.vehicle']
        Vehicle.create({'name': 'LIC-T1', 'vehicle_type': 'trailer'})._ksw_set_custodian(cls.heavy)
        Vehicle.create({'name': 'LIC-T2', 'vehicle_type': 'trailer'})._ksw_set_custodian(cls.private)
        Vehicle.create({'name': 'LIC-T3', 'vehicle_type': 'trailer'})._ksw_set_custodian(cls.unlicensed)
        cls.licence = cls.env.ref('KSW_documents.type_driving_licence')

    def _xlsx(self, rows):
        import openpyxl
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = 'تقرير حاملي رخص القيادة من المق'
        for row in (HEADER,) + tuple(rows):
            sheet.append(row)
        out = io.BytesIO()
        book.save(out)
        return base64.b64encode(out.getvalue())

    def _import(self, rows, source='absher_licences'):
        imp = self.env['ksw.document.import'].with_user(self.officer).create({
            'source': source, 'file': self._xlsx(rows), 'file_name': 'حاملي رخص القيادة.xlsx'})
        imp.action_preview()
        return imp

    ROWS = [('2999997701', 'LIC HEAVY', 'نقل ثقيل', 'صالحة', '2031-07-14'),
            ('2999997702', 'LIC PRIVATE', 'خاصة', 'صالحة', '2027-10-07'),
            ('2999997799', 'NOT IN ODOO', 'نقل خفيف', 'صالحة', '2027-12-14')]

    def test_licences_recorded_with_class_and_expiry(self):
        request = self.env['ksw.document.request'].with_user(self.supervisor).create({
            'type_id': self.licence.id, 'employee_id': self.heavy.id, 'reason': 'New driver of LIC-T9'})
        imp = self._import(self.ROWS)
        self.assertTrue(imp.name.startswith('Absher Business: Driving Licence Holders'))
        row = imp.line_ids.filtered(lambda l: l.id_number == '2999997701')
        self.assertEqual((row.employee_id, row.driving_class, row.driving_expiry),
                         (self.heavy, 'نقل ثقيل', date(2031, 7, 14)))
        private = imp.line_ids.filtered(lambda l: l.id_number == '2999997702')
        self.assertIn('LIC-T2', private.warnings)  # a private licence on a trailer
        stranger = imp.line_ids.filtered(lambda l: l.id_number == '2999997799')
        self.assertEqual(stranger.action, 'skip')
        missing = imp.line_ids.filtered(lambda l: l.kind == 'missing')
        self.assertIn(self.unlicensed, missing.employee_id)
        self.assertIn('LIC-T3', missing.filtered(lambda l: l.employee_id == self.unlicensed).warnings)
        self.assertNotIn(self.heavy, missing.employee_id)
        # Nothing is written before Apply.
        self.assertFalse(self.env['ksw.document'].search([('employee_id', '=', self.heavy.id)]))
        imp.action_apply()
        doc = self.env['ksw.document'].search([('employee_id', '=', self.heavy.id), ('state', '=', 'confirmed')])
        self.assertEqual((doc.type_id, doc.number, doc.license_class, doc.expiry_date, doc.source),
                         (self.licence, '2999997701', 'نقل ثقيل', date(2031, 7, 14), 'absher'))
        self.assertTrue(doc.verified_date)
        # The export confirms without HR's button: the supervisor's request closes all the same.
        self.assertEqual((request.state, request.document_id), ('done', doc))
        self.assertFalse(self.env['ksw.document'].search([('employee_id', '=', self.unlicensed.id)]))

    def test_next_export_renews_or_leaves_alone(self):
        self._import(self.ROWS).action_apply()
        again = self._import([self.ROWS[0], ('2999997702', 'LIC PRIVATE', 'نقل خفيف', 'صالحة', '2032-01-01')])
        again.action_apply()
        docs = self.env['ksw.document'].search([('employee_id', 'in', (self.heavy | self.private).ids),
                                                ('state', '=', 'confirmed')])
        self.assertEqual(len(docs), 2)
        renewed = docs.filtered(lambda d: d.employee_id == self.private)
        self.assertEqual((renewed.expiry_date, renewed.license_class), (date(2032, 1, 1), 'نقل خفيف'))
        self.assertIn('Documents unchanged', again.summary)

    def test_licence_file_is_not_read_as_muqeem(self):
        with self.assertRaisesRegex(UserError, 'driving licence export'):
            self._import(self.ROWS, source='muqeem_residents')
