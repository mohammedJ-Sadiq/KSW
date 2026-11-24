from datetime import date, timedelta

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestDocumentAccess(TransactionCase):
    """Vehicle papers are read by whoever opens the vehicle; employee papers
    by HR; everything (and every change) by the officer and administrators."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)

        def user(login, *groups):
            return Users.create({'name': login, 'login': login,
                                 'group_ids': [(6, 0, [cls.env.ref(g).id for g in ('base.group_user',) + groups])]})

        cls.plain = user('acc_plain')
        cls.hr = user('acc_hr', 'hr.group_hr_user')
        cls.officer = user('acc_officer', 'KSW_documents.group_document_officer')
        cls.admin = user('acc_admin', 'base.group_system')
        cls.truck = cls.env['ksw.fleet.vehicle'].create({'name': 'ACC-1', 'vehicle_type': 'isuzu'})
        cls.driver = cls.env['hr.employee'].create({'name': 'ACC Driver'})
        Doc = cls.env['ksw.document']
        cls.vdoc = Doc.create({'type_id': cls.env.ref('KSW_documents.type_insurance').id, 'applies_to': 'vehicle',
                               'vehicle_id': cls.truck.id, 'number': 'INS1',
                               'expiry_date': date.today() + timedelta(days=90)})
        cls.edoc = Doc.create({'type_id': cls.env.ref('KSW_documents.type_driving_licence').id,
                               'applies_to': 'employee', 'employee_id': cls.driver.id, 'number': 'LIC1',
                               'expiry_date': date.today() + timedelta(days=90)})

    def _seen(self, user):
        return self.env['ksw.document'].with_user(user).search([('id', 'in', (self.vdoc | self.edoc).ids)])

    def test_who_reads_what(self):
        self.assertEqual(self._seen(self.plain), self.vdoc)
        self.assertEqual(self._seen(self.hr), self.vdoc | self.edoc)
        self.assertEqual(self._seen(self.officer), self.vdoc | self.edoc)
        self.assertEqual(self._seen(self.admin), self.vdoc | self.edoc)

    def test_the_vehicle_form_shows_its_papers(self):
        data = self.truck.with_user(self.plain).web_read({'x_document_ids': {'fields': {
            'type_id': {}, 'number': {}, 'expiry_date': {}, 'expiry_status': {}, 'state': {}}},
            'x_documents_missing': {}})
        self.assertEqual([d['number'] for d in data[0]['x_document_ids']], ['INS1'])

    def test_only_the_officer_and_admins_change(self):
        with self.assertRaises(AccessError):
            self.vdoc.with_user(self.plain).write({'number': 'X'})
        with self.assertRaises(AccessError):
            self.edoc.with_user(self.hr).write({'number': 'X'})
        with self.assertRaises(AccessError):
            self.env['ksw.document'].with_user(self.plain).create({
                'type_id': self.env.ref('KSW_documents.type_insurance').id, 'applies_to': 'vehicle',
                'vehicle_id': self.truck.id, 'number': 'NEW'})
        self.vdoc.with_user(self.officer).write({'number': 'INS2'})
        self.edoc.with_user(self.admin).write({'number': 'LIC2'})
        self.assertEqual((self.vdoc.number, self.edoc.number), ('INS2', 'LIC2'))
