from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestDocumentRequest(TransactionCase):
    """Someone outside HR asks for an employee's document: he follows his
    request, HR has it on Home, and the confirmed document closes it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)
        cls.officer = Users.create({'name': 'DR Officer', 'login': 'dr_officer', 'group_ids': [
            (6, 0, [cls.env.ref('KSW_documents.group_document_officer').id, cls.env.ref('hr.group_hr_user').id])]})
        cls.supervisor = Users.create({'name': 'DR Supervisor', 'login': 'dr_supervisor',
                                       'group_ids': [(6, 0, [cls.env.ref('base.group_user').id])]})
        cls.other = Users.create({'name': 'DR Other', 'login': 'dr_other',
                                  'group_ids': [(6, 0, [cls.env.ref('base.group_user').id])]})
        cls.driver = cls.env['hr.employee'].create({'name': 'DR Driver'})
        cls.licence = cls.env.ref('KSW_documents.type_driving_licence')
        cls.Request = cls.env['ksw.document.request']

    def _ask(self, user=None):
        return self.Request.with_user(user or self.supervisor).create({
            'type_id': self.licence.id, 'employee_id': self.driver.id, 'reason': 'New driver of T1'})

    def _waiting(self, user):
        return self.Request.with_user(user).search([('x_is_pending_my_action', '=', True)])

    def test_request_waits_for_hr_and_closes_on_confirm(self):
        request = self._ask()
        self.assertTrue(request.name.startswith('DR/'))
        self.assertIn(request, self._waiting(self.officer))
        self.assertNotIn(request, self._waiting(self.supervisor))
        # The requester follows his own, not other people's.
        self.assertEqual(self.Request.with_user(self.supervisor).search([]), request)
        self.assertFalse(self.Request.with_user(self.other).search([]))
        with self.assertRaises(UserError):
            request.with_user(self.supervisor).action_register()
        action = request.with_user(self.officer).action_register()
        doc = self.env['ksw.document'].browse(action['res_id'])
        self.assertEqual((doc.state, doc.employee_id, doc.type_id), ('draft', self.driver, self.licence))
        # One home: the draft does not wait on Home a second time.
        Doc = self.env['ksw.document'].with_user(self.officer)
        self.assertNotIn(doc, Doc.search([('x_is_pending_my_action', '=', True)]))
        doc.with_user(self.officer).write({'number': 'L1', 'expiry_date': fields.Date.today() + timedelta(days=300)})
        doc.with_user(self.officer).action_confirm()
        self.assertEqual((request.state, request.document_id), ('done', doc))
        self.assertNotIn(request, self._waiting(self.officer))
        told = request.message_ids.filtered(lambda m: self.supervisor.partner_id in m.partner_ids)
        self.assertTrue(told)

    def test_a_licence_entered_another_way_closes_it_too(self):
        request = self._ask()
        doc = self.env['ksw.document'].with_user(self.officer).create({
            'type_id': self.licence.id, 'applies_to': 'employee', 'employee_id': self.driver.id,
            'number': 'L2', 'expiry_date': fields.Date.today() + timedelta(days=300)})
        doc.action_confirm()
        self.assertEqual(request.state, 'done')

    def test_one_open_request_and_none_when_valid(self):
        request = self._ask()
        with self.assertRaises(UserError):
            self._ask(self.other)
        request.with_user(self.supervisor).action_cancel()
        self.assertEqual(request.state, 'cancelled')
        with self.assertRaises(UserError):
            request.with_user(self.other).action_cancel()
        doc = self.env['ksw.document'].create({
            'type_id': self.licence.id, 'applies_to': 'employee', 'employee_id': self.driver.id,
            'number': 'L3', 'expiry_date': fields.Date.today() + timedelta(days=300)})
        doc.action_confirm()
        with self.assertRaises(UserError):
            self._ask()

    def test_requester_cannot_edit_it(self):
        request = self._ask()
        with self.assertRaises(AccessError):
            request.with_user(self.supervisor).write({'reason': 'changed'})
