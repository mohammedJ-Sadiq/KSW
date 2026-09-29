# -*- coding: utf-8 -*-
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase, new_test_user


class TestAuditTrail(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.hr_user = new_test_user(
            cls.env, login='audit_hr', groups='base.group_user,hr.group_hr_user')
        cls.hr_manager = new_test_user(
            cls.env, login='audit_hrm', groups='base.group_user,hr.group_hr_manager')
        cls.plain = new_test_user(cls.env, login='audit_plain',
                                  groups='base.group_user')
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Audited Employee', 'user_id': cls.plain.id})
        cls.boss = cls.env['hr.employee'].create({'name': 'Audit Boss'})
        cls.Log = cls.env['ksw.audit.log']

    def _logs(self, **domain):
        return self.Log.search([(k, '=', v) for k, v in domain.items()])

    def _notes(self, record):
        return record.message_ids.filtered(
            lambda m: m.subtype_id == self.env.ref('mail.mt_note'))

    def test_employee_change_logged_and_posted(self):
        self.employee.with_user(self.hr_user).write({'parent_id': self.boss.id})
        log = self._logs(model='hr.employee', res_id=self.employee.id,
                         field_name='parent_id')
        self.assertEqual(len(log), 1)
        self.assertEqual(log.user_id, self.hr_user)
        self.assertEqual(log.new_value, 'Audit Boss')
        self.assertIn('Audit Boss', self._notes(self.employee)[:1].body)

    def test_group_granted_logged(self):
        group = self.env.ref('hr.group_hr_user')
        self.plain.write({'group_ids': [(4, group.id)]})
        log = self._logs(model='res.users', res_id=self.plain.id,
                         field_name='group_ids')
        self.assertTrue(log)
        self.assertIn(group.display_name, log[:1].new_value)
        self.assertNotIn(group.display_name, log[:1].old_value or '')
        self.assertEqual(log[:1].employee_id, self.employee)

    def test_password_change_logged_without_value(self):
        self.plain.write({'password': 'n3w-Secret!x'})
        log = self._logs(model='res.users', res_id=self.plain.id,
                         field_name='password')
        self.assertTrue(log)
        self.assertNotIn('n3w-Secret', (log.old_value or '') + (log.new_value or ''))

    def test_salary_bank_account_change_logged(self):
        bank = self.env['res.partner.bank'].create({
            'acc_number': 'SA0000000000000000000001',
            'partner_id': self.employee.work_contact_id.id,
        })
        self.employee.write({'bank_account_ids': [(4, bank.id)]})
        self.assertTrue(self._logs(model='hr.employee', res_id=self.employee.id,
                                   field_name='bank_account_ids'))
        bank.write({'acc_number': 'SA9999999999999999999999'})
        log = self._logs(model='res.partner.bank', res_id=bank.id,
                         field_name='acc_number')
        self.assertEqual(log.old_value, 'SA0000000000000000000001')
        self.assertEqual(log.new_value, 'SA9999999999999999999999')
        self.assertEqual(log.employee_id, self.employee)

    def test_unrelated_bank_account_not_logged(self):
        partner = self.env['res.partner'].create({'name': 'Supplier'})
        bank = self.env['res.partner.bank'].create({
            'acc_number': 'SA1111', 'partner_id': partner.id})
        bank.write({'acc_number': 'SA2222'})
        self.assertFalse(self._logs(model='res.partner.bank', res_id=bank.id))

    def test_log_is_read_only_and_restricted(self):
        self.employee.write({'job_title': 'Driver'})
        log = self._logs(model='hr.employee', res_id=self.employee.id)[:1]
        self.assertTrue(log.with_user(self.hr_manager).record_name)
        with self.assertRaises(UserError):
            log.with_user(self.hr_manager).write({'new_value': 'x'})
        with self.assertRaises(UserError):
            log.with_user(self.hr_manager).unlink()
        with self.assertRaises(AccessError):
            log.with_user(self.hr_user).read(['new_value'])

    def test_wage_hidden_in_chatter(self):
        version = self.employee.version_id
        if 'wage' not in version._fields:
            return
        version.write({'wage': 12345.0})
        log = self._logs(model='hr.version', res_id=version.id, field_name='wage')
        self.assertEqual(log.new_value, '12345.0')
        note = self._notes(self.employee)[:1]
        self.assertNotIn('12345', note.body or '')
