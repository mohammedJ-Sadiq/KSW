import base64
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestDocuments(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)
        cls.officer = Users.create({'name': 'DOC Officer', 'login': 'doc_officer', 'group_ids': [
            (6, 0, [cls.env.ref('KSW_documents.group_document_officer').id, cls.env.ref('hr.group_hr_user').id])]})
        cls.plain = Users.create({'name': 'DOC Plain', 'login': 'doc_plain',
                                  'group_ids': [(6, 0, [cls.env.ref('base.group_user').id])]})
        cls.truck = cls.env['ksw.fleet.vehicle'].create({'name': 'DOC-1', 'vehicle_type': 'isuzu',
                                                         'plate_number': 'ABC 1234',
                                                         'x_registration_type': 'public'})
        cls.saudi = cls.env.ref('base.sa')
        cls.driver = cls.env['hr.employee'].create({'name': 'DOC Driver Ahmed', 'country_id': cls.env.ref('base.in').id})
        cls.truck._ksw_set_custodian(cls.driver)
        cls.T = {code: cls.env['ksw.document.type'].search([('code', '=', code)]) for code in
                 ('istimara', 'insurance', 'mvpi', 'operation_card', 'driving_licence', 'driver_card',
                  'iqama', 'passport', 'visa', 'water_tank_licence')}

    def _scan(self, name='scan.png', mimetype='image/png'):
        # As the officer: in the screen he uploads it himself.
        return self.env['ir.attachment'].with_user(self.officer).create(
            {'name': name, 'raw': b'\x89PNG fake', 'mimetype': mimetype})

    def _doc(self, code, owner, expiry, number='111', confirm=True, **extra):
        vals = dict(type_id=self.T[code].id, number=number, expiry_date=expiry,
                    attachment_ids=[(6, 0, self._scan().ids)], **extra)
        if owner._name == 'ksw.fleet.vehicle':
            vals.update(applies_to='vehicle', vehicle_id=owner.id)
        else:
            vals.update(applies_to='employee', employee_id=owner.id)
        doc = self.env['ksw.document'].with_user(self.officer).create(vals)
        if confirm:
            doc.action_confirm()
        return doc

    def test_vehicle_requirements(self):
        self.assertTrue(self.truck.x_document_issue)
        for name in ('Istimara', 'Insurance', 'Periodic Inspection', 'Operation Card'):
            self.assertIn(name, self.truck.x_documents_missing)
        far = fields.Date.today() + timedelta(days=200)
        for code in ('istimara', 'insurance', 'mvpi', 'operation_card', 'water_tank_licence'):
            self._doc(code, self.truck, far)
        self.truck.invalidate_recordset()
        self.assertFalse(self.truck.x_document_issue)
        self.assertNotIn(self.truck, self.env['ksw.fleet.vehicle'].search([('x_document_issue', '=', True)]))

    def test_expired_counts_as_missing_and_alerts_hr(self):
        doc = self._doc('insurance', self.truck, fields.Date.today() - timedelta(days=1))
        self.assertEqual(doc.expiry_status, 'expired')
        self.assertIn('Insurance (expired)', self.truck.x_documents_missing)
        soon = self._doc('istimara', self.truck, fields.Date.today() + timedelta(days=10))
        self.assertEqual(soon.expiry_status, 'expiring')
        self.env['ksw.document']._cron_document_alerts()
        self.assertIn(self.officer, soon.activity_ids.user_id)
        self.assertIn(soon, self.env['ksw.document'].with_user(self.officer).search([('x_is_pending_my_action', '=', True)]))
        # Renewed: the old one is replaced, its to-do goes.
        renewed = self._doc('istimara', self.truck, fields.Date.today() + timedelta(days=400), number='222')
        self.assertEqual(soon.state, 'replaced')
        self.env['ksw.document']._cron_document_alerts()
        self.assertFalse(soon.activity_ids)
        self.assertEqual(renewed.expiry_status, 'valid')

    def test_employee_requirements_follow_the_rules(self):
        missing = self.driver.x_documents_missing
        for name in ('Driving Licence', 'Driver Card', 'Iqama', 'Passport'):
            self.assertIn(name, missing)  # holds a vehicle, non-Saudi
        saudi_clerk = self.env['hr.employee'].create({'name': 'DOC Saudi', 'country_id': self.saudi.id})
        self.assertFalse(saudi_clerk.x_documents_missing)
        unknown = self.env['hr.employee'].create({'name': 'DOC Unknown'})
        self.assertEqual(unknown.x_documents_missing, 'Nationality not recorded')

    def test_confirm_locks_and_writes_back(self):
        draft = self._doc('passport', self.driver, fields.Date.today() + timedelta(days=900), number='P123',
                          confirm=False)
        with self.assertRaises(UserError):
            draft.with_user(self.plain).action_confirm()
        draft.action_confirm()
        self.assertEqual(self.driver.passport_id, 'P123')
        self.assertEqual(self.driver.passport_expiration_date, draft.expiry_date)
        with self.assertRaises(UserError):
            draft.with_user(self.officer).write({'number': 'X'})
        with self.assertRaises(UserError):  # a document needs its dates (a scan is optional)
            self.env['ksw.document'].with_user(self.officer).create({
                'applies_to': 'vehicle', 'vehicle_id': self.truck.id, 'type_id': self.T['insurance'].id,
                'number': '9'}).action_confirm()

    def test_ai_reader_parses_and_flags(self):
        Reader = self.env['ksw.document.ai.reader']
        types = self.T['istimara'] | self.T['insurance']
        vals = Reader._parse('Here: {"type_code": "istimara", "number": "  778899 ", "expiry_date": "2027-03-05", '
                             '"expiry_date_as_printed": "1448/09/06", "confidence": "medium", '
                             '"doubts": ["Seal covers part of the number"], "plate": "XYZ 999"}', types)
        self.assertEqual(vals['type_id'], self.T['istimara'].id)
        self.assertEqual(vals['number'], '778899')
        self.assertEqual(str(vals['expiry_date']), '2027-03-05')
        self.assertTrue(any('1448/09/06' in n for n in vals['notes']))
        self.assertTrue(any('medium' in n for n in vals['notes']))
        self.assertIn('Seal covers part of the number', vals['notes'])
        empty = Reader._parse('sorry', types)
        self.assertTrue(empty['notes'])

    def test_upload_with_ai_makes_a_checked_draft(self):
        read = {'type_id': self.T['istimara'].id, 'number': '778899',
                'expiry_date': fields.Date.today() + timedelta(days=300), 'notes': [],
                '_holder': None, '_plate': 'XYZ 999'}
        Wizard = self.env['ksw.document.upload.wizard'].with_user(self.officer)
        with patch.object(type(self.env['ksw.document.ai.reader']), '_read', return_value=dict(read)):
            wizard = Wizard.create({'applies_to': 'vehicle', 'vehicle_id': self.truck.id, 'use_ai': True,
                                    'attachment_ids': [(6, 0, self._scan().ids)]})
            action = wizard.action_upload()
        doc = self.env['ksw.document'].browse(action['res_id'])
        self.assertEqual((doc.state, doc.number, doc.ai_read), ('draft', '778899', True))
        self.assertIn('XYZ 999', doc.ai_notes)  # plate does not match ABC 1234
        self.assertTrue(self.truck.x_document_issue)  # a draft is not current yet

    def test_upload_without_ai_needs_the_type(self):
        wizard = self.env['ksw.document.upload.wizard'].with_user(self.officer).create({
            'applies_to': 'employee', 'employee_id': self.driver.id, 'use_ai': False,
            'attachment_ids': [(6, 0, self._scan().ids)]})
        with self.assertRaises(UserError):
            wizard.action_upload()
        wizard.type_id = self.T['driving_licence']
        doc = self.env['ksw.document'].browse(wizard.action_upload()['res_id'])
        self.assertEqual((doc.type_id, doc.state), (self.T['driving_licence'], 'draft'))

    def test_only_hr_is_asked(self):
        pending = [('x_document_pending_my_action', '=', True)]
        self.assertIn(self.truck, self.env['ksw.fleet.vehicle'].with_user(self.officer).search(pending))
        self.assertFalse(self.env['ksw.fleet.vehicle'].with_user(self.plain).search(pending))
