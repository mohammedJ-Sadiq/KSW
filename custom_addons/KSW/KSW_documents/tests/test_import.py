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
T196 = ('220\tأ ص ر 8935\tT9196\tالمصنع\tنقل عام\t-\tمرسيدس\tراس تريلا\t2020\t2430120\tTSTB9634031C013456\t'
        'ازرق\tصالحة\t-\t1449-01-19\t1449-02-19\t2999999701\tAKBAR ALI MUHAMMAD ISRAR\tساري المفعول\t'
        'ساري المفعول\tغير متحفظ\t1446-01-19\tصالحة\t4 عجلات')
IS386 = ('128\tأ ط د 4253\tIS9386\tمالك\tنقل عام\t-\tايسوزو\tREWARD\t2025\t526343220\tTSTKP34T1S7P29672\t'
         'ابيض\tصالحة\t-\t1450-04-14\t-\t2999999702\tMOSHAROF SARKER\tلا يوجد\tساري المفعول\tغير متحفظ\t'
         '1447-04-14\tصالحة\t3 عجلات')
BOX = ('69\tأ ص ب 1710\tعبوات\tعبوات\tنقل عام\t-\tايسوزو\tصندوق\t2023\t217498020\tTST6M133PW000562\tابيض\t'
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
                         ('أ ص ر 8935', 'TSTB9634031C013456', 'مرسيدس', 2020))
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
        self.assertEqual(box.name, 'أ ص ب 1710')

    def test_next_month_renewal_and_missing_vehicle(self):
        self._import(T196, IS386).action_apply()
        renewed = T196.replace('1449-01-19\t1449-02-19', '1449-01-19\t1450-02-19')
        imp = self._import(renewed)
        rows = imp.line_ids.filtered(lambda l: l.kind == 'row')
        self.assertEqual(rows.match_note, 'Chassis No.')
        missing = imp.line_ids.filtered(lambda l: l.kind == 'missing')
        self.assertEqual(missing.vehicle_id, self.isuzu)
        imp.action_apply()
        mvpi = self.env['ksw.document'].search([('vehicle_id', '=', self.head.id), ('type_id.code', '=', 'mvpi'),
                                                ('state', '=', 'confirmed')])
        self.assertEqual(mvpi.expiry_date, parse_date('1450-02-19'))
        self.assertIn('Documents renewed', rows.result)
        self.assertIn('Documents unchanged', rows.result)
        self.assertTrue(self.isuzu.active)  # never archived by the import

    def test_absher_never_overwrites_odoo(self):
        self.head.plate_number = 'ABC 1'
        imp = self._import(T196)
        line = imp.line_ids
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
