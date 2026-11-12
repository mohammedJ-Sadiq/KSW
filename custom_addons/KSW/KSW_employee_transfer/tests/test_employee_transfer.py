from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestEmployeeTransfer(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)

        def _user(login, group):
            user = Users.create({'name': login, 'login': login,
                                 'group_ids': [(6, 0, [cls.env.ref(group).id])]})
            cls.env['hr.employee'].create({'name': login, 'user_id': user.id})
            return user

        cls.manager = _user('et_manager', 'KSW_employee_transfer.group_transfer_requester')
        cls.hr = _user('et_hr', 'KSW_employee_transfer.group_transfer_hr')
        cls.gm = _user('et_gm', 'KSW_employee_transfer.group_transfer_gm')
        Department = cls.env['hr.department']
        cls.dept_a = Department.create({'name': 'ET Alpha', 'manager_id': cls.manager.employee_id.id})
        cls.dept_b = Department.create({'name': 'ET Bravo'})
        cls.employee = cls.env['hr.employee'].create({'name': 'ET Worker', 'department_id': cls.dept_a.id})
        cls.outsider = cls.env['hr.employee'].create({'name': 'ET Outsider', 'department_id': cls.dept_b.id})

    def _request(self, **vals):
        return self.env['ksw.employee.transfer'].with_user(self.manager).create(dict({
            'employee_id': self.employee.id, 'to_department_id': self.dept_b.id, 'reason': 'Needed there',
        }, **vals))

    def test_full_route_applies_today(self):
        transfer = self._request()
        self.assertEqual(transfer.from_department_id, self.dept_a)
        transfer.action_submit()
        with self.assertRaises(UserError):
            transfer.with_user(self.manager).action_hr_approve()
        transfer.with_user(self.hr).action_hr_approve()
        with self.assertRaises(UserError):
            transfer.with_user(self.hr).action_gm_approve()
        transfer.with_user(self.gm).action_gm_approve()
        self.assertEqual(transfer.state, 'applied')
        self.assertEqual(self.employee.department_id, self.dept_b)
        self.assertEqual(transfer.sudo().version_id.department_id, self.dept_b)

    def test_future_date_waits_for_the_cron(self):
        tomorrow = fields.Date.today() + timedelta(days=1)
        transfer = self._request(effective_date=tomorrow)
        transfer.action_submit()
        transfer.with_user(self.hr).action_hr_approve()
        transfer.with_user(self.gm).action_gm_approve()
        self.assertEqual(transfer.state, 'approved')
        self.assertEqual(self.employee.department_id, self.dept_a)
        # The version exists from its date on; the days before keep Alpha.
        transfer.sudo().effective_date = fields.Date.today()
        self.env['ksw.employee.transfer']._cron_apply_due()
        self.assertEqual(transfer.state, 'applied')

    def test_history_kept_in_versions(self):
        old_version = self.employee.current_version_id
        transfer = self._request(effective_date=fields.Date.today() + timedelta(days=3))
        transfer.action_submit()
        transfer.with_user(self.hr).action_hr_approve()
        transfer.with_user(self.gm).action_gm_approve()
        transfer.sudo()._apply()
        self.assertEqual(old_version.department_id, self.dept_a)
        self.assertNotEqual(transfer.sudo().version_id, old_version)

    def test_custodian_cannot_move_without_his_vehicles(self):
        vehicle = self.env['ksw.fleet.vehicle'].create({'name': 'ET-1', 'vehicle_type': 'isuzu'})
        vehicle._ksw_set_custodian(self.employee)
        transfer = self._request()
        self.assertIn('ET-1', transfer.warning_text)
        transfer.action_submit()
        with self.assertRaises(UserError):
            transfer.with_user(self.hr).action_hr_approve()
        # Moving together with them is the package's business, allowed.
        transfer.sudo().with_vehicles = True
        transfer.with_user(self.hr).action_hr_approve()

    def test_requester_scope(self):
        with self.assertRaises(UserError):
            self._request(employee_id=self.outsider.id, to_department_id=self.dept_a.id)
        mine = self._request()
        self.assertIn(mine, self.env['ksw.employee.transfer'].with_user(self.manager).search([]))

    def test_refuse_needs_the_step_holder(self):
        transfer = self._request()
        transfer.action_submit()
        wizard = self.env['ksw.employee.transfer.refuse.wizard'].with_user(self.hr).create(
            {'transfer_id': transfer.id, 'reason': 'Not now'})
        wizard.action_confirm()
        self.assertEqual(transfer.state, 'refused')
        self.assertEqual(self.employee.department_id, self.dept_a)

    def test_gm_without_hr_rights_applies_a_dated_transfer(self):
        """A new contract version on another date, approved by a GM who has
        no HR rights: hr.employee.create_version would refuse him."""
        yesterday = fields.Date.today() - timedelta(days=1)
        transfer = self._request(effective_date=yesterday)
        transfer.action_submit()
        transfer.with_user(self.hr).action_hr_approve()
        self.assertFalse(self.gm.has_group('hr.group_hr_user'))
        transfer.with_user(self.gm).action_gm_approve()
        self.assertEqual(transfer.state, 'applied')
        self.assertEqual(transfer.sudo().version_id.date_version, yesterday)
        self.assertEqual(transfer.sudo().version_id.department_id, self.dept_b)
        # The requester still reads his transfer (the version field is HR's).
        transfer.with_user(self.manager).web_read({'name': {}, 'state': {}, 'to_department_id': {'fields': {'display_name': {}}}})
