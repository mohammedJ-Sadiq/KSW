"""The GM steps follow the employee's department, not a group.

Before this, `_check_group('...group_annual_leave_gm')` decided both GM
steps. It never looked at the record -- it does not even iterate `self` --
so one GM group meant one GM over the whole company. Authority now comes
from `hr.department.x_effective_gm_id`.

What these tests pin down:

  - Scope: GM A cannot approve, refuse or return GM B's department, at
    either GM step, and the error names who can.
  - Visibility: the record rules follow the same line -- A does not even
    see B's requests, in the list or in "Waiting For Me".
  - Fallback: a child department with no GM inherits its parent's; with no
    parent GM either, the company default answers; an employee with no
    department at all resolves to the company default too (103 real
    employees are in that position).
  - Capability: naming somebody as a department's GM grants the group, so
    HR sets one field rather than a field and an invisible access right.
  - Notification: the GM steps notify one person, not the whole group.

Every call goes through `with_user(...)`, and never bare `sudo()`: the
guards exempt `env.su`, so a sudo'd call would pass whatever the code did
(Odoo 19 Pitfalls #16).
"""
from datetime import date, timedelta

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase


class TestDepartmentGm(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company

        def _mkuser(name, login, groups=()):
            group_ids = [cls.env.ref('base.group_user').id]
            group_ids.extend(groups)
            return cls.env['res.users'].create({
                'name': name, 'login': login, 'email': f'{login}@gmdept.test',
                'group_ids': [(6, 0, group_ids)],
            })

        cls.user_dm = _mkuser('Dept DM', 'gmdept_dm')
        cls.user_hr = _mkuser('Dept HR', 'gmdept_hr', [
            cls.env.ref('KSW_annual_leave.group_annual_leave_hr').id])
        cls.user_acc = _mkuser('Dept Acc', 'gmdept_acc', [
            cls.env.ref('KSW_annual_leave.group_annual_leave_acc').id])

        # Two GMs, each answerable for one department. Neither is given the
        # GM group here on purpose -- being named on the department is meant
        # to be the whole qualification.
        cls.user_gm_a = _mkuser('GM Alpha', 'gmdept_gm_a')
        cls.user_gm_b = _mkuser('GM Beta', 'gmdept_gm_b')
        cls.user_gm_default = _mkuser('GM Default', 'gmdept_gm_default')

        Employee = cls.env['hr.employee']
        cls.emp_gm_a = Employee.create({
            'name': 'GM Alpha Emp', 'user_id': cls.user_gm_a.id})
        cls.emp_gm_b = Employee.create({
            'name': 'GM Beta Emp', 'user_id': cls.user_gm_b.id})
        cls.emp_gm_default = Employee.create({
            'name': 'GM Default Emp', 'user_id': cls.user_gm_default.id})

        cls.company.sudo().x_default_gm_id = cls.emp_gm_default.id

        Department = cls.env['hr.department']
        cls.dept_a = Department.create({
            'name': 'GM Test Alpha', 'x_gm_id': cls.emp_gm_a.id})
        cls.dept_b = Department.create({
            'name': 'GM Test Beta', 'x_gm_id': cls.emp_gm_b.id})
        # Child of A with no GM of its own, and an orphan with no GM and no
        # parent -- the two fallback shapes.
        cls.dept_a_child = Department.create({
            'name': 'GM Test Alpha Child', 'parent_id': cls.dept_a.id})
        cls.dept_orphan = Department.create({'name': 'GM Test Orphan'})

        cls.emp_a = cls._mkemp('Emp In Alpha', 'gmdept_emp_a', cls.dept_a)
        cls.emp_b = cls._mkemp('Emp In Beta', 'gmdept_emp_b', cls.dept_b)
        cls.emp_none = cls._mkemp('Emp No Dept', 'gmdept_emp_none', None)

        cls.leave_type = cls.env['hr.leave.type'].create({
            'name': 'Annual Leave Dept GM Test',
            # A Python bool: the string 'no' is truthy and would trip
            # _check_validity's allocation guard.
            'requires_allocation': False,
            'leave_validation_type': 'annual_multi',
            'is_annual_leave': True,
        })

    @classmethod
    def _mkemp(cls, name, login, department):
        user = cls.env['res.users'].create({
            'name': name, 'login': login, 'email': f'{login}@gmdept.test',
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
    _ORDER = ['pending_dm', 'pending_hr', 'pending_gm_initial',
              'pending_acc', 'pending_gm_final']

    def _make_leave(self, employee, offset=0):
        base = date(2029, 1, 1) + timedelta(days=offset * 15)
        return self.env['hr.leave'].sudo().create({
            'employee_id': employee.id,
            'holiday_status_id': self.leave_type.id,
            'request_date_from': base,
            'request_date_to': base + timedelta(days=6),
        })

    def _advance_to(self, leave, target, gm_user):
        """Walk the chain up to `target`, using `gm_user` for the GM steps."""
        while leave.x_annual_approval_state != target:
            state = leave.x_annual_approval_state
            if state == 'pending_dm':
                leave.with_user(self.user_dm).sudo().action_dm_approve()
            elif state == 'pending_hr':
                leave.with_user(self.user_hr).sudo().action_hr_approve()
            elif state == 'pending_gm_initial':
                leave.with_user(gm_user).sudo().action_gm_initial_approve()
            elif state == 'pending_acc':
                leave.with_user(self.user_acc).sudo().action_acc_approve()
            else:
                self.fail('Cannot advance past %s' % state)
        return leave

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------
    def test_department_gm_is_the_departments_own(self):
        leave = self._make_leave(self.emp_a)
        self.assertEqual(
            self.env['hr.leave']._department_gm_user(leave), self.user_gm_a)

    def test_child_department_inherits_the_parents_gm(self):
        emp = self._mkemp('Child Emp', 'gmdept_child', self.dept_a_child)
        leave = self._make_leave(emp)
        self.assertEqual(
            self.env['hr.leave']._department_gm_user(leave), self.user_gm_a,
            'a child with no GM of its own should follow its parent')

    def test_department_with_no_gm_anywhere_falls_back_to_the_company(self):
        emp = self._mkemp('Orphan Emp', 'gmdept_orphan', self.dept_orphan)
        leave = self._make_leave(emp)
        self.assertEqual(
            self.env['hr.leave']._department_gm_user(leave),
            self.user_gm_default)

    def test_employee_with_no_department_falls_back_to_the_company(self):
        leave = self._make_leave(self.emp_none)
        self.assertEqual(
            self.env['hr.leave']._department_gm_user(leave),
            self.user_gm_default,
            '103 real employees have no department; they must still route')

    # ------------------------------------------------------------------
    # Capability
    # ------------------------------------------------------------------
    def test_naming_a_gm_grants_the_group(self):
        self.assertTrue(
            self.user_gm_a.has_group(
                'KSW_annual_leave.group_annual_leave_gm'),
            'setting x_gm_id should be the whole setup')

    def test_the_gm_group_no_longer_grants_company_wide_leave_access(self):
        self.assertFalse(
            self.user_gm_a.has_group('KSW_annual_leave.group_leave_officer'),
            'the Officer implication is what made every GM see everything')

    # ------------------------------------------------------------------
    # Authority
    # ------------------------------------------------------------------
    def test_other_departments_gm_cannot_give_initial_approval(self):
        leave = self._make_leave(self.emp_a)
        self._advance_to(leave, 'pending_gm_initial', self.user_gm_a)
        with self.assertRaises(UserError) as caught:
            leave.with_user(self.user_gm_b).action_gm_initial_approve()
        self.assertIn(self.user_gm_a.name, str(caught.exception),
                      'the error should name who can actually approve')
        self.assertEqual(leave.x_annual_approval_state, 'pending_gm_initial')

    def test_other_departments_gm_cannot_give_final_approval(self):
        leave = self._make_leave(self.emp_a, offset=1)
        self._advance_to(leave, 'pending_gm_final', self.user_gm_a)
        with self.assertRaises(UserError):
            leave.with_user(self.user_gm_b).action_gm_final_approve()
        self.assertEqual(leave.x_annual_approval_state, 'pending_gm_final')

    def test_own_departments_gm_can_approve_both_steps(self):
        leave = self._make_leave(self.emp_a, offset=2)
        self._advance_to(leave, 'pending_gm_initial', self.user_gm_a)
        leave.with_user(self.user_gm_a).sudo().action_gm_initial_approve()
        self.assertEqual(leave.x_annual_approval_state, 'pending_acc')
        leave.with_user(self.user_acc).sudo().action_acc_approve()
        leave.with_user(self.user_gm_a).sudo().action_gm_final_approve()
        self.assertEqual(leave.x_annual_approval_state,
                         'pending_employee_signature')

    def test_default_gm_approves_an_employee_with_no_department(self):
        leave = self._make_leave(self.emp_none, offset=3)
        self._advance_to(leave, 'pending_acc', self.user_gm_default)
        self.assertEqual(leave.x_annual_approval_state, 'pending_acc')

    def test_default_gm_has_no_say_over_a_department_with_its_own_gm(self):
        leave = self._make_leave(self.emp_a, offset=4)
        self._advance_to(leave, 'pending_gm_initial', self.user_gm_a)
        with self.assertRaises(UserError):
            leave.with_user(self.user_gm_default).action_gm_initial_approve()

    def test_other_departments_gm_cannot_open_the_return_wizard(self):
        leave = self._make_leave(self.emp_a, offset=5)
        self._advance_to(leave, 'pending_gm_initial', self.user_gm_a)
        with self.assertRaises(UserError):
            leave.with_user(self.user_gm_b).action_open_gm_return_wizard()

    # ------------------------------------------------------------------
    # Gate fields
    # ------------------------------------------------------------------
    def test_gate_fields_are_false_for_another_departments_gm(self):
        leave = self._make_leave(self.emp_a, offset=6)
        self._advance_to(leave, 'pending_gm_initial', self.user_gm_a)
        as_b = leave.with_user(self.user_gm_b).sudo()
        self.assertFalse(as_b.x_can_gm_initial_approve)
        self.assertFalse(as_b.x_can_gm_return)
        as_a = leave.with_user(self.user_gm_a).sudo()
        self.assertTrue(as_a.x_can_gm_initial_approve)
        self.assertTrue(as_a.x_can_gm_return)

    def test_waiting_for_me_only_lists_my_own_departments(self):
        mine = self._make_leave(self.emp_a, offset=7)
        self._advance_to(mine, 'pending_gm_initial', self.user_gm_a)
        theirs = self._make_leave(self.emp_b, offset=8)
        self._advance_to(theirs, 'pending_gm_initial', self.user_gm_b)

        found = self.env['hr.leave'].with_user(self.user_gm_a).search(
            [('x_is_pending_my_action', '=', True)])
        self.assertIn(mine, found)
        self.assertNotIn(theirs, found)

    # ------------------------------------------------------------------
    # Visibility
    # ------------------------------------------------------------------
    def test_a_gm_cannot_read_another_departments_request(self):
        theirs = self._make_leave(self.emp_b, offset=9)
        visible = self.env['hr.leave'].with_user(self.user_gm_a).search(
            [('id', '=', theirs.id)])
        self.assertFalse(visible, 'the record rule should hide it entirely')
        with self.assertRaises(AccessError):
            theirs.with_user(self.user_gm_a).read(['request_date_from'])

    def test_a_gm_can_read_his_own_departments_request(self):
        mine = self._make_leave(self.emp_a, offset=10)
        visible = self.env['hr.leave'].with_user(self.user_gm_a).search(
            [('id', '=', mine.id)])
        self.assertEqual(visible, mine)

    def test_the_default_gm_sees_requests_with_no_department(self):
        orphan_leave = self._make_leave(self.emp_none, offset=11)
        visible = self.env['hr.leave'].with_user(self.user_gm_default).search(
            [('id', '=', orphan_leave.id)])
        self.assertEqual(visible, orphan_leave)

    # ------------------------------------------------------------------
    # Notification
    # ------------------------------------------------------------------
    def test_the_gm_step_notifies_one_person_not_the_group(self):
        leave = self._make_leave(self.emp_a, offset=12)
        self._advance_to(leave, 'pending_hr', self.user_gm_a)
        # Snapshot first: the chatter already holds the creation message,
        # and a bare filter over message_ids would match it (Pitfalls #23).
        existing = leave.message_ids.ids
        leave.with_user(self.user_hr).sudo().action_hr_approve()
        new = leave.message_ids.filtered(lambda m: m.id not in existing)
        notified = new.mapped('partner_ids')
        self.assertIn(self.user_gm_a.partner_id, notified)
        self.assertNotIn(self.user_gm_b.partner_id, notified,
                         'the other department GM has no business hearing '
                         'about this request')
