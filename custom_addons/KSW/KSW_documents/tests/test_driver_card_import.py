import base64
import io
from datetime import date, datetime

from odoo.tests import TransactionCase, tagged

from ..models.ksw_document_import import card_number

MAIN = ('رقم الاقامة', 'الاسم', 'نوع رخصة القيادة', 'حالة رخصة القيادة', 'تاريخ الانتهاء (ميلادي)',
        'رقم بطاقة السائق', 'نوع البطاقة', 'حالة البطاقة', 'تاريخ انتهاء البطاقة (ميلادي)')
CARDS = ('رقم الإقامة ', 'رقم بطاقة السائق', 'تاريخ الانتهاء', 'نوع البطاقة', 'حالة البطاقة ')


@tagged('post_install', '-at_install')
class TestDriverCardImport(TransactionCase):
    """Naql ▸ Driver cards (Nov 2026 workbook: the licence report repeats
    some cards beside each licence, the card sheet lists them all)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.officer = cls.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'DC Officer', 'login': 'dc_officer', 'group_ids': [(6, 0, [
                cls.env.ref('KSW_documents.group_document_officer').id, cls.env.ref('hr.group_hr_user').id])]})
        Employee = cls.env['hr.employee']
        cls.both = Employee.create({'name': 'DC Both Sheets', 'ssnid': '2999996601'})
        cls.card_only = Employee.create({'name': 'DC Card Sheet Only', 'ssnid': '2999996602'})
        cls.no_card = Employee.create({'name': 'DC No Card', 'ssnid': '2999996603'})
        Vehicle = cls.env['ksw.fleet.vehicle']
        public = {'vehicle_type': 'trailer', 'x_registration_type': 'public'}
        Vehicle.create(dict(public, name='DC-T1'))._ksw_set_custodian(cls.both)
        Vehicle.create(dict(public, name='DC-T3'))._ksw_set_custodian(cls.no_card)
        cls.private_driver = Employee.create({'name': 'DC Private Car', 'ssnid': '2999996604'})
        Vehicle.create({'name': 'DC-P4', 'vehicle_type': 'other', 'x_registration_type': 'private'}
                       )._ksw_set_custodian(cls.private_driver)
        cls.card = cls.env.ref('KSW_documents.type_driver_card')

    def _xlsx(self):
        import openpyxl
        book = openpyxl.Workbook()
        main = book.active
        main.title = 'حاملي رخص القيادة'
        for row in (MAIN,
                    ('2999996601', 'DC BOTH', 'نقل ثقيل', 'صالحة', '2031-07-14', 11.006728, 'سنوية', 'نشط',
                     '2027-09-09'),
                    ('2999996603', 'DC NO CARD', 'نقل ثقيل', 'صالحة', '2030-01-01', 'غير موجود', 'غير موجود',
                     'غير موجود', 'غير موجود')):
            main.append(row)
        cards = book.create_sheet('بطاقات السائق')
        for row in (CARDS, (2999996601, 11.006728, datetime(2027, 9, 9), 'سنوية', 'نشط'),
                    (2999996602, 33.00084991, datetime(2026, 12, 29), 'مقيدة', 'نشط')):
            cards.append(row)
        out = io.BytesIO()
        book.save(out)
        return base64.b64encode(out.getvalue())

    def test_card_required_only_for_public_transport_drivers(self):
        types = (self.no_card | self.private_driver)._ksw_required_types()
        self.assertIn(self.card, types[self.no_card.id])
        self.assertNotIn(self.card, types[self.private_driver.id])

    def test_card_number_from_excel_decimal(self):
        self.assertEqual(card_number(11.006728), '1100672800')
        self.assertEqual(card_number('33.00084991'), '3300084991')
        self.assertEqual(card_number('1100651875'), '1100651875')
        self.assertEqual(card_number('غير موجود'), '')

    def test_cards_from_every_sheet_recorded_once(self):
        imp = self.env['ksw.document.import'].with_user(self.officer).create({
            'source': 'naql_driver_cards', 'file': self._xlsx(), 'file_name': 'بطاقات السائقين.xlsx'})
        imp.action_preview()
        rows = imp.line_ids.filtered(lambda l: l.kind == 'row')
        self.assertEqual(sorted(rows.mapped('id_number')), ['2999996601', '2999996602'])  # listed twice = once
        both = rows.filtered(lambda l: l.employee_id == self.both)
        self.assertEqual((both.card_number, both.card_expiry), ('1100672800', date(2027, 9, 9)))
        missing = imp.line_ids.filtered(lambda l: l.kind == 'missing')
        self.assertIn(self.no_card, missing.employee_id)
        self.assertIn('DC-T3', missing.filtered(lambda l: l.employee_id == self.no_card).warnings)
        self.assertNotIn(self.both, missing.employee_id)
        self.assertNotIn(self.private_driver, missing.employee_id)  # a private car needs no Naql card
        imp.action_apply()
        docs = self.env['ksw.document'].search([('type_id', '=', self.card.id), ('state', '=', 'confirmed'),
                                                ('employee_id', 'in', (self.both | self.card_only).ids)])
        self.assertEqual(len(docs), 2)
        restricted = docs.filtered(lambda d: d.employee_id == self.card_only)
        self.assertEqual((restricted.number, restricted.expiry_date, restricted.source),
                         ('3300084991', date(2026, 12, 29), 'naql'))
        self.assertIn('مقيدة', restricted.ai_notes)
        self.assertTrue(restricted.verified_date)
