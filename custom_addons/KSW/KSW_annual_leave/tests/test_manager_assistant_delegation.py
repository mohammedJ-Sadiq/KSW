"""Tests: the Manager Assistant delegation on time off.

A direct manager nominates an assistant (`res.users.x_assistant_ids`) who may
**prepare** requests for that manager's direct reports. The manager keeps every
approval step personally. The whole feature is two keys that must both turn:
the manager names the assistant, AND an administrator grants
`KSW_base_security.group_manager_assistant`. Neither works alone.

The invariants pinned down here:

  * scope is the delegated manager's **direct reports** only — not the wider
    company, and not the manager's own requests;
  * the edit window closes the moment **any** approver signs, for every leave
    type: the multi-step chains keep `state == 'confirm'` past GM final
    approval, so the gate has to read `x_annual_approval_state` for those and
    `state` for ordinary types;
  * the assistant can never approve, refuse or confirm a return; they may
    delete a request they prepared, but only before the manager's own first
    approval — the window closes the moment that step (or the DM step of a
    plain leave type) is signed off (August 2026);
  * an undelegated holder of the group sees nothing, because
    `_ksw_assisted_manager_ids()` returns an empty list;
  * `employee_id` cannot be re-pointed out of the delegation — record rules
    are evaluated on the pre-write values only, so that needs a Python guard.
"""
from datetime import date, timedelta

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase


class TestManagerAssistantDelegation(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _mkuser(name, login, groups=()):
            return cls.env['res.users'].create({
                'name': name,
                'login': login,
                'email': f'{login}@asstdeleg.test',
                'group_ids': [(6, 0, [cls.env.ref('base.group_user').id]
                               + [cls.env.ref(g).id for g in groups])],
            })

        cls.ASSISTANT_GROUP = 'KSW_base_security.group_manager_assistant'

        # The delegating manager, and a second manager nobody delegates for.
        cls.user_mgr = _mkuser('Deleg Manager', 'asstdeleg_mgr')
        cls.emp_mgr = cls.env['hr.employee'].create({
            'name': 'Deleg Manager Emp', 'user_id': cls.user_mgr.id})

        cls.user_other_mgr = _mkuser('Deleg Other Manager', 'asstdeleg_omgr')
        cls.emp_other_mgr = cls.env['hr.employee'].create({
            'name': 'Deleg Other Manager Emp',
            'user_id': cls.user_other_mgr.id})

        # Two direct reports of the delegating manager. parent_id drives the
        # record-rule scope, leave_manager_id drives the DM approval action.
        cls.emp_r1 = cls.env['hr.employee'].create({
            'name': 'Deleg Report One',
            'user_id': _mkuser('Deleg R1', 'asstdeleg_r1').id,
            'parent_id': cls.emp_mgr.id,
            'leave_manager_id': cls.user_mgr.id,
        })
        cls.emp_r2 = cls.env['hr.employee'].create({
            'name': 'Deleg Report Two',
            'user_id': _mkuser('Deleg R2', 'asstdeleg_r2').id,
            'parent_id': cls.emp_mgr.id,
            'leave_manager_id': cls.user_mgr.id,
        })
        # Somebody else's report — must stay invisible throughout.
        cls.emp_other = cls.env['hr.employee'].create({
            'name': 'Deleg Outsider',
            'user_id': _mkuser('Deleg Outsider U', 'asstdeleg_out').id,
            'parent_id': cls.emp_other_mgr.id,
            'leave_manager_id': cls.user_other_mgr.id,
        })

        # Key one: the group. Key two: the nomination.
        cls.user_asst = _mkuser('Deleg Assistant', 'asstdeleg_asst',
                                (cls.ASSISTANT_GROUP,))
        # leave_manager_id set so the assistant's OWN request still produces a
        # pending_dm notification -- otherwise the "no Prepared by" assertion
        # below would pass vacuously (no recipients, no message at all).
        cls.emp_asst = cls.env['hr.employee'].create({
            'name': 'Deleg Assistant Emp', 'user_id': cls.user_asst.id,
            'leave_manager_id': cls.user_mgr.id})
        cls.user_mgr.sudo().write({'x_assistant_ids': [(4, cls.user_asst.id)]})

        # Holds the group, nominated by nobody: the second key is missing.
        cls.user_stranger = _mkuser('Deleg Stranger', 'asstdeleg_stranger',
                                    (cls.ASSISTANT_GROUP,))
        cls.env['hr.employee'].create({
            'name': 'Deleg Stranger Emp', 'user_id': cls.user_stranger.id})

        cls.user_hr = _mkuser('Deleg HR', 'asstdeleg_hr',
                              ('KSW_annual_leave.group_annual_leave_hr',))

        cls.annual_type = cls.env['hr.leave.type'].create({
            'name': 'Annual Leave Assistant Test',
            # Python False, not the truthy string 'no' (Pitfalls #24).
            'requires_allocation': False,
            'leave_validation_type': 'annual_multi',
            'is_annual_leave': True,
        })
        cls.plain_type = cls.env['hr.leave.type'].create({
            'name': 'Plain Leave Assistant Test',
            'requires_allocation': False,
            'leave_validation_type': 'hr',
        })

    def setUp(self):
        super().setUp()
        self._slot = 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _dates(self):
        self._slot += 1
        start = date.today() + timedelta(days=10 * self._slot)
        return start, start + timedelta(days=3)

    def _leave_as(self, user, employee, leave_type=None):
        """Create a leave as `user` — no sudo, so the create rule applies."""
        start, end = self._dates()
        return self.env['hr.leave'].with_user(user).create({
            'employee_id': employee.id,
            'holiday_status_id': (leave_type or self.annual_type).id,
            'request_date_from': start,
            'request_date_to': end,
        })

    def _leave_sudo(self, employee, leave_type=None):
        start, end = self._dates()
        return self.env['hr.leave'].sudo().create({
            'employee_id': employee.id,
            'holiday_status_id': (leave_type or self.annual_type).id,
            'request_date_from': start,
            'request_date_to': end,
        })

    # ------------------------------------------------------------------
    # Scope
    # ------------------------------------------------------------------

    def test_read_scope_covers_delegated_team_only(self):
        mine = self._leave_sudo(self.emp_r1)
        theirs = self._leave_sudo(self.emp_other)
        managers_own = self._leave_sudo(self.emp_mgr)

        visible = self.env['hr.leave'].with_user(self.user_asst).search([])
        self.assertIn(mine, visible,
                      "assistant must see the delegated manager's report")
        self.assertNotIn(theirs, visible,
                         "assistant must not see another manager's team")
        self.assertNotIn(
            managers_own, visible,
            "the manager's own request is deliberately out of scope")

    def test_undelegated_group_holder_sees_nothing(self):
        """Second key missing: the group alone grants no scope."""
        leave = self._leave_sudo(self.emp_r1)
        visible = self.env['hr.leave'].with_user(self.user_stranger).search([])
        self.assertNotIn(leave, visible)
        self.assertEqual(
            self.user_stranger._ksw_assisted_manager_ids(), [],
            "an undelegated user must resolve to an empty manager list")

    def test_helper_requires_the_group(self):
        """First key missing: the nomination alone grants no scope."""
        plain = self.env['res.users'].create({
            'name': 'Deleg No Group', 'login': 'asstdeleg_nogrp',
            'email': 'asstdeleg_nogrp@asstdeleg.test',
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.user_mgr.sudo().write({'x_assistant_ids': [(4, plain.id)]})
        self.assertEqual(plain._ksw_assisted_manager_ids(), [])

    def test_employee_picker_scope(self):
        domain = self.env['hr.leave'].with_user(
            self.user_asst)._get_employee_domain()
        pickable = self.env['hr.employee'].sudo().search(domain)
        self.assertIn(self.emp_r1, pickable)
        self.assertIn(self.emp_r2, pickable)
        self.assertNotIn(self.emp_other, pickable)
        self.assertNotIn(self.emp_mgr, pickable)

    # ------------------------------------------------------------------
    # Create + edit
    # ------------------------------------------------------------------

    def test_create_for_delegated_report(self):
        leave = self._leave_as(self.user_asst, self.emp_r1)
        self.assertTrue(leave.id)
        self.assertEqual(
            leave.sudo().x_annual_approval_state, 'pending_dm',
            "a delegated request must start at the manager's own step")

    def test_create_outside_delegation_denied(self):
        with self.assertRaises(UserError):
            self._leave_as(self.user_asst, self.emp_other)

    def test_edit_while_pending_dm(self):
        leave = self._leave_as(self.user_asst, self.emp_r1)
        new_end = leave.request_date_to + timedelta(days=1)
        leave.with_user(self.user_asst).write({'request_date_to': new_end})
        self.assertEqual(leave.sudo().request_date_to, new_end)

    def test_edit_after_dm_approval_denied(self):
        leave = self._leave_as(self.user_asst, self.emp_r1)
        # with_user first so _check_group sees the DM, sudo only to widen
        # record-rule scope (Pitfalls #16).
        leave.with_user(self.user_mgr).sudo().action_dm_approve()
        self.assertEqual(leave.sudo().x_annual_approval_state, 'pending_hr')
        with self.assertRaises(AccessError):
            leave.with_user(self.user_asst).write(
                {'request_date_to': leave.sudo().request_date_to
                 + timedelta(days=1)})

    def test_edit_after_first_approval_denied_plain_type(self):
        """Ordinary types progress via `state`, not the KSW chain."""
        leave = self._leave_as(self.user_asst, self.emp_r1, self.plain_type)
        leave.sudo().write({'state': 'validate'})
        with self.assertRaises(AccessError):
            leave.with_user(self.user_asst).write(
                {'request_date_to': leave.sudo().request_date_to
                 + timedelta(days=1)})

    def test_employee_reassignment_blocked(self):
        """Record rules only see pre-write values; the guard closes this."""
        leave = self._leave_as(self.user_asst, self.emp_r1)
        with self.assertRaises(UserError):
            leave.with_user(self.user_asst).write(
                {'employee_id': self.emp_other.id})

    def test_reassignment_within_delegation_allowed(self):
        leave = self._leave_as(self.user_asst, self.emp_r1)
        leave.with_user(self.user_asst).write({'employee_id': self.emp_r2.id})
        self.assertEqual(leave.sudo().employee_id, self.emp_r2)

    # ------------------------------------------------------------------
    # Every leave type, not just annual
    # ------------------------------------------------------------------

    def test_all_leave_types_delegated(self):
        for leave_type in (self.annual_type, self.plain_type):
            with self.subTest(leave_type=leave_type.name):
                leave = self._leave_as(
                    self.user_asst, self.emp_r1, leave_type)
                self.assertTrue(leave.id)
                self.assertEqual(leave.sudo().employee_id, self.emp_r1)

    # ------------------------------------------------------------------
    # No authority, ever
    # ------------------------------------------------------------------

    def test_cannot_dm_approve(self):
        leave = self._leave_as(self.user_asst, self.emp_r1)
        self.assertFalse(
            leave.with_user(self.user_asst).x_can_dm_approve,
            "the DM-approve button must not even render for an assistant")
        with self.assertRaises(UserError):
            leave.with_user(self.user_asst).action_dm_approve()

    def test_cannot_approve_plain_leave(self):
        leave = self._leave_as(self.user_asst, self.emp_r1, self.plain_type)
        with self.assertRaises(UserError):
            leave.with_user(self.user_asst).action_approve()

    def test_cannot_refuse(self):
        leave = self._leave_as(self.user_asst, self.emp_r1)
        with self.assertRaises(UserError):
            leave.with_user(self.user_asst).action_refuse()

    def test_can_delete_before_dm_approval(self):
        """August 2026: the assistant may withdraw a request they prepared
        as long as the manager hasn't acted on it yet — same window as
        edit (test_edit_while_pending_dm)."""
        leave = self._leave_as(self.user_asst, self.emp_r1)
        leave_id = leave.id
        leave.with_user(self.user_asst).unlink()
        self.assertFalse(self.env['hr.leave'].sudo().browse(leave_id).exists())

    def test_cannot_delete_after_dm_approval(self):
        leave = self._leave_as(self.user_asst, self.emp_r1)
        leave.with_user(self.user_mgr).sudo().action_dm_approve()
        self.assertEqual(leave.sudo().x_annual_approval_state, 'pending_hr')
        with self.assertRaises(AccessError):
            leave.with_user(self.user_asst).unlink()

    def test_cannot_delete_plain_leave_after_first_approval(self):
        """Ordinary types progress via `state`, not the KSW chain."""
        leave = self._leave_as(self.user_asst, self.emp_r1, self.plain_type)
        leave.sudo().write({'state': 'validate'})
        with self.assertRaises(AccessError):
            leave.with_user(self.user_asst).unlink()

    def test_cannot_hr_approve(self):
        leave = self._leave_as(self.user_asst, self.emp_r1)
        leave.with_user(self.user_mgr).sudo().action_dm_approve()
        with self.assertRaises(UserError):
            leave.with_user(self.user_asst).action_hr_approve()

    # ------------------------------------------------------------------
    # Provenance
    # ------------------------------------------------------------------

    def test_manager_notified_names_the_preparer(self):
        leave = self._leave_as(self.user_asst, self.emp_r1)
        bodies = leave.sudo().message_ids.mapped('body')
        self.assertTrue(
            any('Prepared by' in b and self.user_asst.name in b
                for b in bodies),
            "the DM's notification must name whoever filed the request")

    def test_self_filed_request_has_no_prepared_by(self):
        leave = self._leave_as(self.user_asst, self.emp_asst)
        bodies = leave.sudo().message_ids.mapped('body')
        self.assertTrue(
            any('Action Required' in b for b in bodies),
            "guard the assertion below against a vacuous pass")
        self.assertFalse(
            any('Prepared by' in b for b in bodies),
            "no preparer line when the employee filed their own request")
