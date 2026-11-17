import base64
from datetime import date

from odoo.tests import TransactionCase, tagged

from odoo.addons.KSW_documents.models.hijri import parse_date
from odoo.addons.KSW_documents.models.ksw_document_import import fleet_key

HEADER = ('العدد\tرقم اللوحة\tالكود\tالقسم\tنوع التسجيل\tالفرع\tالماركة\tالطراز\tسنة الصنع\tالرقم التسلسلي\t'
          'رقم الهيكل\tاللون الأساسي\tوضع المركبة\tتاريخ الملكية\tتاريخ انتهاء رخصة السير\tتاريخ انتهاء الفحص\t'
          'رقم هوية المستخدم الفعلي\tاسم المستخدم الفعلي\tحالة الفحص\tحالة التأمين\tحالة التحفظ\t'
          'تاريخ إصدار الاستمارة\tوضع المركبة\tنوع الهيكل')
# Rows as the real export prints them (Nov 2026), chassis numbers made unique to the test.
T196 = ('220\tق ك ل 9902\tT9196\tالمصنع\tنقل عام\t-\tمرسيدس\tراس تريلا\t2020\t2430120\tTSTB9634031C013456\t'
        'ازرق\tصالحة\t-\t1449-01-19\t1449-02-19\t2999999701\tAKBAR ALI MUHAMMAD ISRAR\tساري المفعول\t'
        'ساري المفعول\tغير متحفظ\t1446-01-19\tصالحة\t4 عجلات')
IS386 = ('128\tق ك د 9903\tIS9386\tمالك\tنقل عام\t-\tايسوزو\tREWARD\t2025\t526343220\tTSTKP34T1S7P29672\t'
         'ابيض\tصالحة\t-\t1450-04-14\t-\t2999999702\tMOSHAROF SARKER\tلا يوجد\tساري المفعول\tغير متحفظ\t'
         '1447-04-14\tصالحة\t3 عجلات')
BOX = ('69\tق ك ب 9904\tعبوات\tعبوات\tنقل عام\t-\tايسوزو\tصندوق\t2023\t217498020\tTST6M133PW000562\tابيض\t'
       'صالحة\t-\t1448-11-21\t1448-04-28\t-\t-\tساري المفعول\tلا يوجد\tغير متحفظ\t1445-11-21\tصالحة\t4 عجلات')


@tagged('post_install', '-at_install')
class TestAbsherImport(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.officer = cls.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'IMP Officer', 'login': 'imp_officer', 'group_ids': [(6, 0, [
                cls.env.ref('KSW_documents.group_document_officer').id, cls.env.ref('hr.group_hr_user').id])]})
        Vehicle = cls.env['ksw.fleet.vehicle']
        # As in production: the fleet number only, zero-padded.
        cls.head = Vehicle.create({'name': 'T9196', 'vehicle_type': 'trailer'})
        cls.isuzu = Vehicle.create({'name': 'IS9386', 'vehicle_type': 'isuzu'})
        cls.driver = cls.env['hr.employee'].create({'name': 'IMP Akbar', 'ssnid': '2999999701'})

    def _import(self, *rows):
        text = '\n'.join((HEADER,) + rows)
        imp = self.env['ksw.document.import'].with_user(self.officer).create({
            'file': base64.b64encode(text.encode()), 'file_name': 'absher.csv'})
        imp.action_preview()
        return imp

    def test_hijri_and_fleet_keys(self):
        self.assertEqual(parse_date('1448-06-06'), date(2026, 11, 16))
        self.assertIsNone(parse_date('-'))
        self.assertEqual(fleet_key('T099'), fleet_key('T99'))
        self.assertEqual(fleet_key('س 43'), fleet_key('43 س'))
        self.assertIsNone(fleet_key('محمد سهيل'))

    def test_preview_matches_by_fleet_code_and_writes_nothing(self):
        imp = self._import(T196, IS386, BOX)
        rows = {l.code: l for l in imp.line_ids}
        self.assertEqual(rows['T9196'].vehicle_id, self.head)
        self.assertEqual(rows['T9196'].match_note, 'Fleet No.')
        self.assertEqual(rows['IS9386'].vehicle_id, self.isuzu)
        # A generic code is not a key: that vehicle is proposed for creation.
        self.assertEqual((rows['عبوات'].vehicle_id, rows['عبوات'].action), (self.env['ksw.fleet.vehicle'], 'create'))
        self.assertIn('insurance', rows['عبوات'].warnings)
        self.assertEqual(rows['T9196'].istimara_expiry, parse_date('1449-01-19'))
        self.assertEqual(rows['T9196'].user_employee_id, self.driver)
        self.assertFalse(self.head.plate_number)
        self.assertFalse(self.env['ksw.document'].search([('vehicle_id', '=', self.head.id)]))

    def test_apply_fills_vehicle_and_records_documents_without_scans(self):
        imp = self._import(T196, IS386, BOX)
        imp.action_apply()
        self.assertEqual(imp.state, 'done')
        self.assertEqual((self.head.plate_number, self.head.x_vin, self.head.brand, self.head.year),
                         ('ق ك ل 9902', 'TSTB9634031C013456', 'مرسيدس', 2020))
        self.assertEqual(self.head.x_plate_en, 'LKG 9902')
        docs = self.env['ksw.document'].search([('vehicle_id', '=', self.head.id), ('state', '=', 'confirmed')])
        by_code = {d.type_id.code: d for d in docs}
        self.assertEqual(by_code['istimara'].expiry_date, parse_date('1449-01-19'))
        self.assertEqual(by_code['istimara'].number, '2430120')
        self.assertEqual(by_code['mvpi'].expiry_date, parse_date('1449-02-19'))
        self.assertEqual(by_code['insurance'].expiry_status, 'valid')  # Absher: valid, no date
        self.assertEqual(set(docs.mapped('source')), {'absher'})
        # A new vehicle is not inspected yet: nothing due.
        mvpi = self.env['ksw.document'].search([('vehicle_id', '=', self.isuzu.id), ('type_id.code', '=', 'mvpi')])
        self.assertEqual(mvpi.expiry_status, 'valid')
        # The generic-code box truck was created, named by its plate.
        box = self.env['ksw.fleet.vehicle'].search([('x_vin', '=', 'TST6M133PW000562')])
        self.assertEqual(box.name, 'ق ك ب 9904')

    def test_next_month_renewal_and_missing_vehicle(self):
        self._import(T196, IS386).action_apply()
        renewed = T196.replace('1449-01-19\t1449-02-19', '1449-01-19\t1450-02-19')
        imp = self._import(renewed)
        rows = imp.line_ids.filtered(lambda l: l.kind == 'row')
        self.assertEqual(rows.match_note, 'Chassis No.')
        missing = imp.line_ids.filtered(lambda l: l.kind == 'missing')
        self.assertIn(self.isuzu, missing.vehicle_id)
        self.assertNotIn(self.head, missing.vehicle_id)
        imp.action_apply()
        mvpi = self.env['ksw.document'].search([('vehicle_id', '=', self.head.id), ('type_id.code', '=', 'mvpi'),
                                                ('state', '=', 'confirmed')])
        self.assertEqual(mvpi.expiry_date, parse_date('1450-02-19'))
        self.assertIn('Documents renewed', rows.result)
        self.assertIn('Documents unchanged', rows.result)
        self.assertTrue(self.isuzu.active)  # never archived by the import

    def test_digits_only_plate_is_matched_not_duplicated(self):
        pickup = self.env['ksw.fleet.vehicle'].create({'name': 'بكب ايسوزو 9904', 'plate_number': '9904'})
        imp = self._import(BOX)
        row = imp.line_ids.filtered(lambda l: l.kind == 'row')
        self.assertEqual((row.vehicle_id, row.action, row.match_note), (pickup, 'update', 'Plate digits'))
        imp.action_apply()
        self.assertEqual((pickup.plate_number, pickup.x_plate_en), ('ق ك ب 9904', 'BKG 9904'))

    def test_absher_never_overwrites_odoo(self):
        self.head.plate_number = 'ABC 1'
        imp = self._import(T196)
        line = imp.line_ids.filtered(lambda l: l.kind == 'row')
        self.assertIn('Plate in Odoo', line.warnings)
        imp.action_apply()
        self.assertEqual(self.head.plate_number, 'ABC 1')

    def test_old_scan_never_replaces_newer(self):
        self._import(T196).action_apply()
        Document = self.env['ksw.document'].with_user(self.officer)
        scan = self.env['ir.attachment'].with_user(self.officer).create(
            {'name': 'old.png', 'raw': b'x', 'mimetype': 'image/png'})
        old = Document.create({'type_id': self.env.ref('KSW_documents.type_mvpi').id, 'applies_to': 'vehicle',
                               'vehicle_id': self.head.id, 'number': 'OLD', 'expiry_date': '2025-01-01',
                               'attachment_ids': [(6, 0, scan.ids)]})
        old.action_confirm()
        self.assertEqual(old.state, 'replaced')
        current = Document.search([('vehicle_id', '=', self.head.id), ('type_id.code', '=', 'mvpi'),
                                   ('state', '=', 'confirmed')])
        self.assertEqual(current.expiry_date, parse_date('1449-02-19'))

    def test_waiting_on_home_until_applied(self):
        imp = self._import(T196)
        Import = self.env['ksw.document.import'].with_user(self.officer)
        self.assertIn(imp, Import.search([('x_is_pending_my_action', '=', True)]))
        imp.action_apply()
        self.assertNotIn(imp, Import.search([('x_is_pending_my_action', '=', True)]))


MUQEEM_HEADER = ('رقم الاقامة\tالاسم\tالجنس\tالجنسية\tالمهنة\tرقم الجواز\tتاريخ انتهاء الجواز\t'
                 'تاريخ اصدار الاقامة\tتاريخ انتهاء الاقامة\tتاريخ الميلاد\tخارج المملكه\t'
                 'تاريخ انتهاء الاقامة بالهجري\tرقم صاحب العمل')


@tagged('post_install', '-at_install')
class TestMuqeemGosiImport(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.officer = cls.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'MQ Officer', 'login': 'mq_officer', 'group_ids': [(6, 0, [
                cls.env.ref('KSW_documents.group_document_officer').id, cls.env.ref('hr.group_hr_user').id])]})
        Registration = cls.env['ksw.company.registration']
        cls.main = Registration.create({'name': 'MQ Main', 'kind': 'main', 'unified_number': '7999998800'})
        cls.hayah = Registration.create({'name': 'مصنع تجربة الاستيراد (test)', 'kind': 'branch',
                                         'unified_number': '7999998801'})
        Employee = cls.env['hr.employee']
        cls.machingal = Employee.create({'name': 'MQ Machingal', 'ssnid': '2999998801'})
        cls.saudi = Employee.create({'name': 'MQ Ruqayya', 'ssnid': '1999998802'})
        cls.left = Employee.create({'name': 'MQ Left', 'ssnid': '2999998803'})

    def _import(self, source, text, name='export.csv'):
        imp = self.env['ksw.document.import'].with_user(self.officer).create({
            'source': source, 'file': base64.b64encode(text.encode()), 'file_name': name})
        imp.action_preview()
        return imp

    def test_muqeem_records_iqama_passport_and_registration(self):
        rows = [MUQEEM_HEADER,
                '2999998801\tMACHINGAL - - ABDULAZEEZ\tذكر\tالهند\tسائق شاحنة\tW5501794\t2033-01-11\t'
                '1992-01-31\t2026-12-06\t1964-07-01\tنعم\t1448-06-26\t7999998800',
                '2999998899\tNOT IN ODOO\tذكر\tمصر\tعامل\tA1\t2030-01-01\t2020-01-01\t2027-01-01\t'
                '1990-01-01\tلا\t1448-07-01\t7999998800']
        imp = self._import('muqeem_residents', '\n'.join(rows))
        self.assertTrue(imp.name.startswith('Muqeem'))
        row = imp.line_ids.filtered(lambda l: l.id_number == '2999998801')
        self.assertEqual((row.employee_id, row.line_registration_id), (self.machingal, self.main))
        self.assertIn('Outside the Kingdom', row.warnings)
        stranger = imp.line_ids.filtered(lambda l: l.id_number == '2999998899')
        self.assertEqual(stranger.action, 'skip')
        self.assertIn('Not an employee', stranger.warnings)
        missing = imp.line_ids.filtered(lambda l: l.kind == 'missing')
        self.assertIn(self.left, missing.employee_id)
        self.assertNotIn(self.saudi, missing.employee_id)  # Saudis are not in Muqeem
        imp.action_apply()
        self.assertEqual(self.machingal.x_registration_id, self.main)
        self.assertEqual(self.machingal.country_id, self.env.ref('base.in'))
        docs = self.env['ksw.document'].search([('employee_id', '=', self.machingal.id), ('state', '=', 'confirmed')])
        by = {d.type_id.code: d for d in docs}
        self.assertEqual((by['iqama'].number, str(by['iqama'].expiry_date)), ('2999998801', '2026-12-06'))
        self.assertEqual((by['passport'].number, str(by['passport'].expiry_date)), ('W5501794', '2033-01-11'))
        self.assertEqual(set(docs.mapped('source')), {'muqeem'})
        self.assertEqual(self.machingal.passport_id, 'W5501794')
        self.assertTrue(self.left.active)  # never archived by the import

    def test_gosi_finds_establishment_and_ignores_wages(self):
        text = ('\tشركة مصنع تجربة الاستيراد\tأسم المنشأة:\t\n'
                '600393227\tرقم إشتراك المنشأة:\t\n\n'
                'اسم المشترك\tرقم الهوية\tالجنسية\tالجنس\tتاريخ الميلاد\tالأجر الأساسي\tالسكن\tالعمولات\t'
                'البدلات الأخرى\tإجمالي الأجر\tالاجر الخاضع للاشتراك\tالمهنة\tتاريخ الإلتحاق\t'
                'الاهلية لنظام التأمينات الاجتماعية 1445هـ\n'
                'رقيه بنت علي\t1999998802\tالسعودية \tانثى\t1979-11-18\t4000\t0\t0\t0\t4000\t4000\t'
                'كاتب استعلامات\t2024-11-07\tنعم')
        imp = self._import('gosi_subscribers', text)
        self.assertEqual(imp.registration_id, self.hayah)
        self.assertEqual(imp.line_ids.employee_id, self.saudi)
        self.assertFalse(imp.line_ids.filtered(lambda l: l.kind == 'missing'))
        imp.action_apply()
        self.assertEqual(self.saudi.x_registration_id, self.hayah)
        self.assertNotIn('4000', str(imp.line_ids.read()))
