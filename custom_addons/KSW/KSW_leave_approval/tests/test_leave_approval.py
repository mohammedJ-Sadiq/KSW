from odoo.tests.common import TransactionCase
from odoo.exceptions import AccessError, UserError
from odoo import fields

class TestLeaveApproval(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Create users. Record-rule scope on hr.leave comes exclusively from
        # the KSW_annual_leave tier groups (Pitfalls #38 — the stock
        # hr_holidays rules are all neutralised) — a bare
        # hr_holidays.group_hr_holidays_user/_manager grants NO read/write/
        # unlink scope by itself, only UI/menu visibility and the is_officer
        # shortcut in core's own _get_next_states_by_state.
        cls.hr_manager_user = cls.env['res.users'].create({
            'name': 'HR Manager User',
            'login': 'hr_manager_user',
            'group_ids': [(4, cls.env.ref('KSW_annual_leave.group_leave_officer').id)]
        })
        cls.dm_user = cls.env['res.users'].create({
            'name': 'Direct Manager User',
            'login': 'dm_user',
            'group_ids': [(4, cls.env.ref('KSW_annual_leave.group_leave_supervisor').id)]
        })
        # Reproduces the production incident: a Direct Manager who was also
        # (incorrectly) handed the core Time-Off Administrator group, which
        # implies group_hr_holidays_user (is_officer) and lets core's
        # action_approve() jump confirm -> validate directly.
        cls.dm_officer_user = cls.env['res.users'].create({
            'name': 'Direct Manager+Officer User',
            'login': 'dm_officer_user',
            'group_ids': [
                (4, cls.env.ref('KSW_annual_leave.group_leave_supervisor').id),
                (4, cls.env.ref('hr_holidays.group_hr_holidays_manager').id),
            ]
        })
        cls.other_user = cls.env['res.users'].create({
            'name': 'Other User',
            'login': 'other_user',
            'group_ids': [(4, cls.env.ref('hr_holidays.group_hr_holidays_user').id)]
        })
        cls.employee_user = cls.env['res.users'].create({
            'name': 'Employee User',
            'login': 'employee_user',
            'group_ids': [(4, cls.env.ref('base.group_user').id)]
        })

        # Set HR Manager in settings
        cls.env.company.x_hr_leave_manager_id = cls.hr_manager_user

        # Create employees
        cls.dm_employee = cls.env['hr.employee'].create({
            'name': 'DM Employee',
            'user_id': cls.dm_user.id,
        })
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Subordinate Employee',
            'user_id': cls.employee_user.id,
            'parent_id': cls.dm_employee.id,
        })

        cls.dm_officer_employee = cls.env['hr.employee'].create({
            'name': 'DM+Officer Employee',
            'user_id': cls.dm_officer_user.id,
        })
        cls.other_employee_user = cls.env['res.users'].create({
            'name': 'Other Subordinate User',
            'login': 'other_subordinate_user',
            'group_ids': [(4, cls.env.ref('base.group_user').id)]
        })
        cls.other_employee = cls.env['hr.employee'].create({
            'name': 'Other Subordinate Employee',
            'user_id': cls.other_employee_user.id,
            'parent_id': cls.dm_officer_employee.id,
        })

        # Non-annual leave type, created fresh (not picked via search()) so
        # its company_id/country_id always satisfy
        # hr_holidays_status_rule_multi_company for every test user,
        # regardless of what demo/other-country leave types exist in the db.
        cls.leave_type = cls.env['hr.leave.type'].create({
            'name': 'KSW Leave Approval Test Type',
            'requires_allocation': False,
            'leave_validation_type': 'both',
            'is_annual_leave': False,
            'company_id': False,
            'country_id': False,
        })
        
    def test_01_non_annual_approval_flow(self):
        """Test strict 2-step approval for non-annual leaves."""
        # Create leave request (starts in 'confirm' state in Odoo 19)
        leave = self.env['hr.leave'].sudo().create({
            'name': 'Sick Leave',
            'holiday_status_id': self.leave_type.id,
            'employee_id': self.employee.id,
            'request_date_from': fields.Date.today(),
            'request_date_to': fields.Date.today(),
        })
        self.assertEqual(leave.state, 'confirm')

        # 1. Other user (not DM) tries to approve -> should fail
        with self.assertRaises(UserError):
            leave.with_user(self.other_user).action_approve()

        # 2. HR Manager (not DM) tries to approve -> should fail
        with self.assertRaises(UserError):
            leave.with_user(self.hr_manager_user).action_approve()

        # 3. DM tries to approve -> should succeed
        leave.with_user(self.dm_user).action_approve()
        self.assertEqual(leave.state, 'validate1')

        # 4. DM (not HR Manager) tries to finish the approval -> should fail.
        # The UI only ever calls action_approve() (both the "Approve" and
        # "Validate" buttons use it — core routes internally to validate1 or
        # straight to validate via _action_validate()), so that's the real
        # path to exercise, not the standalone action_validate() method.
        with self.assertRaises(UserError):
            leave.with_user(self.dm_user).action_approve()
        self.assertEqual(leave.state, 'validate1')

        # 5. HR Manager tries to finish the approval -> should succeed
        leave.with_user(self.hr_manager_user).action_approve()
        self.assertEqual(leave.state, 'validate')

    def test_02_responsible_for_approval(self):
        """Test that the responsible user for activities is correct at each step."""
        leave = self.env['hr.leave'].sudo().create({
            'name': 'Sick Leave',
            'holiday_status_id': self.leave_type.id,
            'employee_id': self.employee.id,
            'request_date_from': fields.Date.today(),
            'request_date_to': fields.Date.today(),
        })
        
        # At confirm state, responsible should be DM
        self.assertEqual(leave._get_responsible_for_approval(), self.dm_user)
        
        # Approve 1st step
        leave.with_user(self.dm_user).action_approve()
        
        # At validate1 state, responsible should be HR Manager
        self.assertEqual(leave._get_responsible_for_approval(), self.hr_manager_user)

    def test_03_officer_dm_cannot_skip_to_final_approval(self):
        """Regression test — KSWCO leave 5008 (August 2026).

        A Direct Manager who ALSO holds a core Time-Off Officer/Administrator
        group (hr_holidays.group_hr_holidays_user or _manager) gets
        can_validate=True from core's own _get_next_states_by_state the
        moment the leave is at 'confirm', for a validation_type=='both' type
        — core lets an "officer" jump confirm -> validate directly, entirely
        skipping validate1. Before the fix, action_approve() only checked
        DM identity for the *state == 'confirm'* transition and then
        delegated straight to core, which silently completed the ENTIRE
        approval — DM step and HR step — in one call by the same person.
        _action_validate() must now block that jump for anyone but the
        configured HR Manager, regardless of which state it's called from.
        """
        leave = self.env['hr.leave'].sudo().create({
            'name': 'Sick Leave',
            'holiday_status_id': self.leave_type.id,
            'employee_id': self.other_employee.id,
            'request_date_from': fields.Date.today(),
            'request_date_to': fields.Date.today(),
        })
        self.assertEqual(leave.state, 'confirm')

        # Sanity check this test actually reproduces the incident: the
        # officer-DM must be seen as able to validate directly by core.
        self.assertTrue(
            leave.with_user(self.dm_officer_user).can_validate,
            "test setup must reproduce the is_officer direct-validate path")

        with self.assertRaises(UserError):
            leave.with_user(self.dm_officer_user).action_approve()

        # Nothing was written — the guard fires before any state change.
        self.assertEqual(leave.state, 'confirm')

    def test_04_dm_can_delete_before_hr_approves(self):
        """DM already approved (validate1); HR hasn't. The DM may withdraw
        the request instead of waiting on HR."""
        leave = self.env['hr.leave'].sudo().create({
            'name': 'Sick Leave',
            'holiday_status_id': self.leave_type.id,
            'employee_id': self.employee.id,
            'request_date_from': fields.Date.today(),
            'request_date_to': fields.Date.today(),
        })
        leave.with_user(self.dm_user).action_approve()
        self.assertEqual(leave.state, 'validate1')

        leave_id = leave.id
        leave.with_user(self.dm_user).unlink()
        self.assertFalse(self.env['hr.leave'].sudo().browse(leave_id).exists())

    def test_05_unrelated_user_cannot_delete(self):
        leave = self.env['hr.leave'].sudo().create({
            'name': 'Sick Leave',
            'holiday_status_id': self.leave_type.id,
            'employee_id': self.employee.id,
            'request_date_from': fields.Date.today(),
            'request_date_to': fields.Date.today(),
        })
        leave.with_user(self.dm_user).action_approve()
        self.assertEqual(leave.state, 'validate1')

        with self.assertRaises(AccessError):
            leave.with_user(self.other_user).unlink()
