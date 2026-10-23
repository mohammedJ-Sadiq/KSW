from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged


@tagged('post_install', '-at_install')
class TestClientMultiplier(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.today = fields.Date.context_today(cls.env['res.users'])
        cls.supervisor = new_test_user(
            cls.env, 'mr_supervisor', groups='base.group_user,KSW_water_delivery.group_water_dispatcher')
        cls.gm = new_test_user(cls.env, 'mr_gm', groups='base.group_user')
        cls.other_gm = new_test_user(cls.env, 'mr_other_gm', groups='base.group_user')
        cls.default_gm = new_test_user(cls.env, 'mr_default_gm', groups='base.group_user')
        cls.accountant = new_test_user(
            cls.env, 'mr_accountant', groups='base.group_user,account.group_account_invoice')
        Emp = cls.env['hr.employee']
        cls.gm_emp = Emp.create({'name': 'GM Water', 'user_id': cls.gm.id})
        cls.other_gm_emp = Emp.create({'name': 'GM Other', 'user_id': cls.other_gm.id})
        cls.default_gm_emp = Emp.create({'name': 'GM Default', 'user_id': cls.default_gm.id})
        cls.dept = cls.env['hr.department'].create({'name': 'Water Ops', 'x_gm_id': cls.gm_emp.id})
        cls.sup_emp = Emp.create({'name': 'Supervisor', 'user_id': cls.supervisor.id,
                                  'department_id': cls.dept.id})
        cls.env.company.x_default_gm_id = cls.default_gm_emp
        cls.customer = cls.env['ksw.bas.customer'].create({
            'bas_code': '1203ZZ001', 'name_ar': 'عميل تجريبي'})
        cls.client = cls.env['ksw.client.multiplier'].create({'customer_id': cls.customer.id})
        cls.env['ksw.client.multiplier.rate'].create({
            'client_id': cls.client.id, 'multiplier': 2.0, 'source': 'opening'})

    def _request(self, new=2.5, days=5, user=None):
        return self.env['ksw.client.multiplier.request'].with_user(user or self.supervisor).create({
            'client_id': self.client.id, 'new_multiplier': new,
            'effective_date': self.today + timedelta(days=days), 'reason': 'Site moved further'})

    def test_opening_value_is_current(self):
        self.assertEqual(self.client.current_multiplier, 2.0)
        self.assertEqual(self.env['ksw.client.multiplier']._multiplier_on(
            ['1203ZZ001', 'UNKNOWN'], '2020-01-01'), {'1203ZZ001': 2.0})

    def test_department_gm_approves_from_effective_date_only(self):
        req = self._request()
        req.with_user(self.supervisor).action_submit()
        self.assertEqual(req.state, 'pending')
        self.assertEqual(req.gm_user_id, self.gm)
        self.assertEqual(req.old_multiplier, 2.0)
        self.assertTrue(req.activity_ids.filtered(lambda a: a.user_id == self.gm))
        # Another GM, the default GM and the requester cannot decide.
        for user in (self.other_gm, self.default_gm, self.supervisor):
            with self.assertRaises(UserError):
                req.with_user(user).action_approve()
        req.with_user(self.gm).action_approve()
        self.assertEqual(req.state, 'approved')
        self.assertFalse(req.activity_ids)
        effective = req.effective_date
        resolve = self.env['ksw.client.multiplier']._multiplier_on
        self.assertEqual(resolve(['1203ZZ001'], effective - timedelta(days=1)), {'1203ZZ001': 2.0})
        self.assertEqual(resolve(['1203ZZ001'], effective), {'1203ZZ001': 2.5})
        # Still the old value today; the next change is announced.
        self.assertEqual(self.client.current_multiplier, 2.0)
        self.assertEqual(self.client.next_multiplier, 2.5)
        self.assertEqual(self.client.next_date, effective)

    def test_effective_today_becomes_current(self):
        req = self._request(new=3.0, days=0)
        req.with_user(self.supervisor).action_submit()
        req.with_user(self.gm).action_approve()
        self.assertEqual(self.client.current_multiplier, 3.0)

    def test_no_department_falls_back_to_default_gm(self):
        self.sup_emp.department_id = False
        req = self._request()
        req.with_user(self.supervisor).action_submit()
        self.assertEqual(req.gm_user_id, self.default_gm)
        req.with_user(self.default_gm).action_approve()
        self.assertEqual(req.state, 'approved')

    def test_parent_department_gm(self):
        child = self.env['hr.department'].create({'name': 'Tabuk', 'parent_id': self.dept.id})
        self.sup_emp.department_id = child
        req = self._request()
        req.with_user(self.supervisor).action_submit()
        self.assertEqual(req.gm_user_id, self.gm)

    def test_refuse_keeps_value(self):
        req = self._request()
        req.with_user(self.supervisor).action_submit()
        wizard = self.env['ksw.client.multiplier.refuse.wizard'].with_user(self.gm).create({
            'request_id': req.id, 'reason': 'No evidence'})
        wizard.action_confirm()
        self.assertEqual(req.state, 'refused')
        self.assertEqual(req.refuse_reason, 'No evidence')
        self.assertEqual(len(self.client.rate_ids), 1)

    def test_only_supervisor_requests(self):
        with self.assertRaises(UserError):
            self._request(user=self.accountant)

    def test_accounting_cannot_change_the_value(self):
        client = self.client.with_user(self.accountant)
        self.assertEqual(client.current_multiplier, 2.0)
        with self.assertRaises(AccessError):
            client.rate_ids.write({'multiplier': 9.0})
        with self.assertRaises(AccessError):
            self.env['ksw.client.multiplier.rate'].with_user(self.accountant).create({
                'client_id': self.client.id, 'multiplier': 9.0, 'date_from': self.today})

    def test_effective_date_cannot_reach_back(self):
        req = self._request(days=-1)
        with self.assertRaises(UserError):
            req.with_user(self.supervisor).action_submit()

    def test_one_pending_request_per_client(self):
        self._request().with_user(self.supervisor).action_submit()
        with self.assertRaises(UserError):
            self._request(new=3.0).with_user(self.supervisor).action_submit()

    def test_sent_request_is_locked(self):
        req = self._request()
        req.with_user(self.supervisor).action_submit()
        with self.assertRaises(UserError):
            req.with_user(self.supervisor).write({'new_multiplier': 6.0})

    def test_gm_sees_only_his_requests(self):
        req = self._request()
        req.with_user(self.supervisor).action_submit()
        Request = self.env['ksw.client.multiplier.request']
        self.assertIn(req, Request.with_user(self.gm).search([]))
        self.assertNotIn(req, Request.with_user(self.other_gm).search([]))

    def test_refresh_seeds_new_clients_only(self):
        new_customer = self.env['ksw.bas.customer'].create({'bas_code': '1203ZZ002', 'name_ar': 'جديد'})
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            {'code': '1203ZZ001', 'FACTORE': 1.25},   # BAS edited: Odoo keeps 2.0
            {'code': '1203ZZ002', 'FACTORE': 1.5},
        ]
        conn = MagicMock()
        conn.cursor.return_value = cursor
        with patch.object(type(self.env['ksw.bas.connector']), '_bas_connect', return_value=conn):
            self.env['ksw.client.multiplier']._refresh_from_bas()
        new_client = self.env['ksw.client.multiplier'].search([('customer_id', '=', new_customer.id)])
        self.assertEqual(new_client.current_multiplier, 1.5)
        self.assertEqual(self.client.current_multiplier, 2.0)
        self.assertEqual(self.client.bas_factor, 1.25)
        self.assertTrue(self.client.differs_from_bas)
