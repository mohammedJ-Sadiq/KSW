"""The accounting step follows the employee's department, not a group.

Before this, `_check_group('...group_annual_leave_acc')` decided the step.
It never looked at the record -- it does not even iterate `self` -- so one
accounting group meant every accountant over every department. Authority
now comes from `hr.department.x_effective_accountant_ids`.

The mirror of test_department_gm.py, with one structural difference that
these tests exist to pin: the accounting step is worked by a TEAM. A
department has one General Manager but several accountants, any of whom may
clear the step. What narrows is which departments a person appears on, so
somebody named on a single department is in that department and nowhere
else -- which is the whole request this feature answers.

What these tests pin down:

  - Scope: an accountant named on department A cannot approve a request
    from department B, and the error names who can.
  - The team: any member of a department's list may clear the step, and
    naming anybody on a department REPLACES the inherited team rather than
    adding to it (otherwise nothing is ever narrowed).
  - Visibility: the record rules follow the same line -- A's accountant
    does not see B's requests in the list or in "Waiting For Me".
  - Fallback: a child department with no accountants inherits its parent's;
    an orphan department and an employee with no department at all fall
    back to the company team.
  - Capability: naming somebody grants the group, so HR sets one field
    rather than a field and an invisible access right.
  - Notification: the step notifies that department's team, not the group.

Every call goes through `with_user(...)`, and never bare `sudo()`: the
guards exempt `env.su`, so a sudo'd call would pass whatever the code did
(Odoo 19 Pitfalls #16).
"""
from datetime import date, timedelta

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestDepartmentAccountant(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company

        def _mkuser(name, login, groups=()):
            group_ids = [cls.env.ref('base.group_user').id]
            group_ids.extend(groups)
            return cls.env['res.users'].create({
                'name': name, 'login': login, 'email': f'{login}@accdept.test',
                'group_ids': [(6, 0, group_ids)],
            })

        cls.user_dm = _mkuser('Acc Dept DM', 'accdept_dm')
        cls.user_hr = _mkuser('Acc Dept HR', 'accdept_hr', [
            cls.env.ref('KSW_annual_leave.group_annual_leave_hr').id])

        # The GM tier is not what is under test; one GM for everything keeps
        # the chain walkable without pulling the GM feature into these
        # assertions.
        cls.user_gm = _mkuser('Acc Dept GM', 'accdept_gm')

        # Two accountants confined to one department each, plus a two-person
        # company-wide team. None of them is given the group here on
        # purpose -- being named is meant to be the whole qualification.
        cls.user_acc_a = _mkuser('Acc Alpha', 'accdept_acc_a')
        cls.user_acc_b = _mkuser('Acc Beta', 'accdept_acc_b')
        cls.user_acc_team1 = _mkuser('Acc Team One', 'accdept_team1')
        cls.user_acc_team2 = _mkuser('Acc Team Two', 'accdept_team2')

        Employee = cls.env['hr.employee']
        cls.emp_gm = Employee.create({
            'name': 'Acc Dept GM Emp', 'user_id': cls.user_gm.id})
        cls.emp_acc_a = Employee.create({
            'name': 'Acc Alpha Emp', 'user_id': cls.user_acc_a.id})
        cls.emp_acc_b = Employee.create({
            'name': 'Acc Beta Emp', 'user_id': cls.user_acc_b.id})
        cls.emp_team1 = Employee.create({
            'name': 'Acc Team One Emp', 'user_id': cls.user_acc_team1.id})
        cls.emp_team2 = Employee.create({
            'name': 'Acc Team Two Emp', 'user_id': cls.user_acc_team2.id})

        cls.company.sudo().write({
            'x_default_gm_id': cls.emp_gm.id,
            'x_default_accountant_ids': [
                (6, 0, [cls.emp_team1.id, cls.emp_team2.id])],
        })

        Department = cls.env['hr.department']
        cls.dept_a = Department.create({
            'name': 'Acc Test Alpha',
            'x_accountant_ids': [(6, 0, [cls.emp_acc_a.id])]})
        cls.dept_b = Department.create({
            'name': 'Acc Test Beta',
            'x_accountant_ids': [(6, 0, [cls.emp_acc_b.id])]})
        # Child of A with no accountants of its own, and an orphan with
        # neither accountants nor a parent -- the two fallback shapes.
        cls.dept_a_child = Department.create({
            'name': 'Acc Test Alpha Child', 'parent_id': cls.dept_a.id})
        cls.dept_orphan = Department.create({'name': 'Acc Test Orphan'})

        cls.emp_a = cls._mkemp('Emp In Alpha', 'accdept_emp_a', cls.dept_a)
        cls.emp_b = cls._mkemp('Emp In Beta', 'accdept_emp_b', cls.dept_b)
        cls.emp_a_child = cls._mkemp(
            'Emp In Alpha Child', 'accdept_emp_ac', cls.dept_a_child)
        cls.emp_orphan = cls._mkemp(
            'Emp In Orphan', 'accdept_emp_o', cls.dept_orphan)
        cls.emp_none = cls._mkemp('Emp No Dept', 'accdept_emp_none', None)

        cls.leave_type = cls.env['hr.leave.type'].create({
            'name': 'Annual Leave Dept Acc Test',
            # A Python bool: the string 'no' is truthy and would trip
            # _check_validity's allocation guard.
            'requires_allocation': False,
            'leave_validation_type': 'annual_multi',
            'is_annual_leave': True,
        })

    @classmethod
    def _mkemp(cls, name, login, department):
        user = cls.env['res.users'].create({
            'name': name, 'login': login, 'email': f'{login}@accdept.test',
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id])],
        })
        vals = {'name': name, 'user_id': user.id,
                'leave_manager_id': cls.user_dm.id}
        if department:
            vals['department_id'] = department.id
        return cls.env['hr.employee'].create(vals)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _make_leave(self, employee, offset=0):
        base = date(2029, 3, 1) + timedelta(days=offset * 15)
        return self.env['hr.leave'].sudo().create({
            'employee_id': employee.id,
            'holiday_status_id': self.leave_type.id,
            'request_date_from': base,
            'request_date_to': base + timedelta(days=6),
        })

    def _advance_to_acc(self, leave):
        """Walk the chain to the accounting step."""
        leave.with_user(self.user_dm).sudo().action_dm_approve()
        leave.with_user(self.user_hr).sudo().action_hr_approve()
        leave.with_user(self.user_gm).sudo().action_gm_initial_approve()
        self.assertEqual(leave.x_annual_approval_state, 'pending_acc')
        return leave

    # ------------------------------------------------------------------
    # Resolution and fallback
    # ------------------------------------------------------------------
    def test_accountants_are_the_departments_own(self):
        leave = self._make_leave(self.emp_a)
        self.assertEqual(
            self.env['hr.leave']._department_accountant_users(leave),
            self.user_acc_a)

    def test_child_department_inherits_the_parents_accountants(self):
        leave = self._make_leave(self.emp_a_child)
        self.assertEqual(
            self.env['hr.leave']._department_accountant_users(leave),
            self.user_acc_a,
            'A department with no accountants of its own inherits its '
            "parent's, not the company team.")

    def test_orphan_department_falls_back_to_the_company_team(self):
        leave = self._make_leave(self.emp_orphan)
        self.assertEqual(
            self.env['hr.leave']._department_accountant_users(leave),
            self.user_acc_team1 | self.user_acc_team2)

    def test_employee_with_no_department_falls_back_to_the_company_team(self):
        leave = self._make_leave(self.emp_none)
        self.assertEqual(
            self.env['hr.leave']._department_accountant_users(leave),
            self.user_acc_team1 | self.user_acc_team2)

    def test_naming_accountants_replaces_the_inherited_team(self):
        """The point of naming somebody is that the team stops applying.

        If the inherited list survived alongside, a department assigned to
        one person would still be worked by everybody and nothing would
        have been narrowed.
        """
        self.assertNotIn(
            self.emp_team1, self.dept_a.x_effective_accountant_ids,
            'Naming an accountant must replace the company team, not add '
            'to it.')
        self.assertEqual(self.dept_a.x_effective_accountant_ids,
                         self.emp_acc_a)

    # ------------------------------------------------------------------
    # Scope: approving
    # ------------------------------------------------------------------
    def test_other_departments_accountant_is_refused(self):
        leave = self._advance_to_acc(self._make_leave(self.emp_b))
        # No sudo() on a refusal: the guard exempts env.su, so a sudo'd
        # call passes whatever the code does (Odoo 19 Pitfalls #16).
        with self.assertRaises(UserError) as caught:
            leave.with_user(self.user_acc_a).action_acc_approve()
        # The message has to name who CAN act: whoever reads it is usually
        # the person who has to go and find them.
        self.assertIn(self.user_acc_b.name, str(caught.exception))
        self.assertEqual(leave.x_annual_approval_state, 'pending_acc')

    def test_own_departments_accountant_approves(self):
        leave = self._advance_to_acc(self._make_leave(self.emp_a))
        leave.with_user(self.user_acc_a).sudo().action_acc_approve()
        self.assertEqual(leave.x_annual_approval_state, 'pending_gm_final')

    def test_any_member_of_the_team_may_clear_the_step(self):
        """A team, not a manager: the second member is as good as the first."""
        leave = self._advance_to_acc(self._make_leave(self.emp_none))
        leave.with_user(self.user_acc_team2).sudo().action_acc_approve()
        self.assertEqual(leave.x_annual_approval_state, 'pending_gm_final')

    def test_company_team_cannot_touch_an_assigned_department(self):
        """The other half of the narrowing, and the one that is easy to miss.

        Confining somebody to one department is worth nothing if the
        company-wide team still answers for that department too.
        """
        leave = self._advance_to_acc(self._make_leave(self.emp_a))
        with self.assertRaises(UserError):
            leave.with_user(self.user_acc_team1).action_acc_approve()

    def test_no_accountant_anywhere_says_so(self):
        leave = self._advance_to_acc(self._make_leave(self.emp_none))
        self.company.sudo().x_default_accountant_ids = [(5, 0, 0)]
        # The guard itself, not the button: with nobody designated the user
        # has no record-rule scope either, so going through the action would
        # raise AccessError first and prove nothing about the message.
        with self.assertRaises(UserError) as caught:
            self.env['hr.leave'].with_user(
                self.user_acc_team1)._check_department_accountant(leave)
        self.assertIn('No Accounting Approver', str(caught.exception))

    # ------------------------------------------------------------------
    # Scope: visibility
    # ------------------------------------------------------------------
    def test_accountant_does_not_see_another_departments_request(self):
        """Not just the button -- the record rule draws the same line."""
        leave_b = self._advance_to_acc(self._make_leave(self.emp_b))
        visible = self.env['hr.leave'].with_user(self.user_acc_a).search(
            [('id', '=', leave_b.id)])
        self.assertFalse(
            visible, "Beta's request must not be readable by Alpha's "
                     'accountant at all.')

    def test_accountant_sees_his_own_departments_request(self):
        leave_a = self._advance_to_acc(self._make_leave(self.emp_a))
        visible = self.env['hr.leave'].with_user(self.user_acc_a).search(
            [('id', '=', leave_a.id)])
        self.assertEqual(visible, leave_a)

    def test_waiting_for_me_is_scoped_to_the_own_department(self):
        leave_a = self._advance_to_acc(self._make_leave(self.emp_a, 1))
        leave_b = self._advance_to_acc(self._make_leave(self.emp_b, 2))
        mine = self.env['hr.leave'].with_user(self.user_acc_a).search(
            [('x_is_pending_my_action', '=', True),
             ('id', 'in', (leave_a | leave_b).ids)])
        self.assertEqual(mine, leave_a)

    def test_pending_my_action_compute_agrees_with_the_search(self):
        """The compute and its search= twin must not disagree.

        A search= that returns [] means "match everything" rather than
        "match nothing" (Odoo 19 Pitfalls #24), so the two are checked
        against each other rather than each on its own.
        """
        leave_a = self._advance_to_acc(self._make_leave(self.emp_a, 3))
        leave_b = self._advance_to_acc(self._make_leave(self.emp_b, 4))
        as_a = self.env['hr.leave'].with_user(self.user_acc_a)
        self.assertTrue(as_a.browse(leave_a.id).x_is_pending_my_action)
        self.assertFalse(
            leave_b.with_user(self.user_acc_a).sudo().x_is_pending_my_action)

    def test_button_gate_follows_the_department(self):
        leave_b = self._advance_to_acc(self._make_leave(self.emp_b, 5))
        as_a = leave_b.with_user(self.user_acc_a).sudo()
        self.assertFalse(as_a.x_can_acc_approve)
        # x_is_acc_approver gates readonly= on the accounting fields, so it
        # has to narrow with the button or a stranger can still type in the
        # figures even though he cannot press Approve.
        self.assertFalse(as_a.x_is_acc_approver)
        as_b = leave_b.with_user(self.user_acc_b).sudo()
        self.assertTrue(as_b.x_can_acc_approve)
        self.assertTrue(as_b.x_is_acc_approver)

    # ------------------------------------------------------------------
    # Capability and notification
    # ------------------------------------------------------------------
    def test_naming_an_accountant_grants_the_group(self):
        """One field, not a field plus an invisible access right."""
        group = self.env.ref('KSW_annual_leave.group_annual_leave_acc')
        self.assertIn(self.user_acc_a, group.sudo().all_user_ids)
        self.assertIn(self.user_acc_team1, group.sudo().all_user_ids,
                      'The company team is reached through res.company, not '
                      'hr.department, so it needs its own capability sync.')

    def test_accountant_without_a_user_account_is_refused(self):
        orphan = self.env['hr.employee'].create({'name': 'No Login Acc'})
        with self.assertRaises(Exception):
            self.dept_orphan.write({'x_accountant_ids': [(4, orphan.id)]})

    def test_step_notifies_the_departments_team_only(self):
        leave = self._make_leave(self.emp_a, 6)
        existing = leave.message_ids.ids
        self._advance_to_acc(leave)
        new = leave.message_ids.filtered(lambda m: m.id not in existing)
        notified = new.mapped('partner_ids')
        self.assertIn(self.user_acc_a.partner_id, notified)
        self.assertNotIn(
            self.user_acc_b.partner_id, notified,
            "Beta's accountant must not hear about Alpha's request.")
        self.assertNotIn(
            self.user_acc_team1.partner_id, notified,
            'The company team does not answer for a department that names '
            'its own accountants.')
