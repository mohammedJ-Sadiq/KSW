# -*- coding: utf-8 -*-
"""Employee self-service on their own deductions.

Two things are covered here:

1. An ordinary employee can OPEN their own deduction. This used to crash:
   the form's ``x_can_submit`` / ``x_can_dm_approve`` computes read
   ``employee_id.parent_id.user_id``, and the "Employee: Own records only"
   rule from KSW_base_security stops an employee reading any hr.employee
   but their own — including their own manager's. Note the fixture below
   gives every test user ``group_hr_employee_subordinate``: WITHOUT it a
   user has no hr.employee ACL at all, falls through to the
   hr.employee.public HACK, and the bug is invisible.

2. They may correct or delete their own loan request, but only while no
   approver has signed it, and only its own fields.
"""
from odoo.exceptions import AccessError, UserError
from odoo.tools import mute_logger

from .common import DeductionCommon


class TestEmployeeSelfService(DeductionCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)
        cls.grp_employee = cls.env.ref(
            'KSW_base_security.group_hr_employee_subordinate')
        cls.grp_ded_user = cls.env.ref('KSW_deduction.group_deduction_user')

        def _mk(login, extra_groups, employee):
            user = Users.create({
                'name': login,
                'login': login,
                'email': f'{login}@kswself.test',
                'group_ids': [(6, 0, [cls.grp_employee.id,
                                      cls.grp_ded_user.id] + extra_groups)],
            })
            employee.write({'user_id': user.id})
            return user

        # cls.employee reports to cls.manager_emp (see DeductionCommon).
        cls.user_employee = _mk('kswself_emp', [], cls.employee)
        cls.user_manager = _mk('kswself_mgr', [], cls.manager_emp)
        cls.other_emp = cls.env['hr.employee'].create({
            'name': 'KSWSELF Other', 'department_id': cls.dept_b.id,
        })
        cls.user_other = _mk('kswself_other', [], cls.other_emp)

    def _own_loan(self):
        return self._make_deduction(self.type_loan, employee=self.employee,
                                    amount=1200.0, installments=3)

    # ------------------------------------------------------------------
    # 1. Reading own records (the reported crash)
    # ------------------------------------------------------------------
    def test_employee_opens_own_loan_form(self):
        """Every field the form loads must be readable by the requester."""
        loan = self._own_loan()
        loan.with_user(self.user_employee).read([
            'name', 'employee_id', 'manager_id', 'department_id', 'amount',
            'x_can_submit', 'x_can_dm_approve', 'x_can_cancel',
            'x_can_employee_edit', 'x_is_pending_my_action',
            'x_emp_monthly_total',
        ])

    def test_employee_opens_own_loan_form_at_pending_dm(self):
        """pending_dm is the state that also walks
        _compute_is_pending_my_action into the manager's employee record."""
        loan = self._own_loan()
        loan.action_submit()
        loan.with_user(self.user_employee).read(
            ['x_can_submit', 'x_can_dm_approve', 'x_is_pending_my_action'])

    def test_employee_reads_own_penalty(self):
        """Self-service is not loans-only: an employee can look up the
        penalty or advance being taken off their salary."""
        pen = self._make_deduction(self.type_gov_pen, employee=self.employee)
        pen.action_submit()
        pen.with_user(self.user_employee).read(['name', 'amount', 'state'])

    def test_manager_opens_subordinates_loan(self):
        """The DM is a plain user too — and reads a record whose employee
        is NOT them."""
        loan = self._own_loan()
        loan.action_submit()
        loan.with_user(self.user_manager).read(
            ['x_can_dm_approve', 'x_is_pending_my_action', 'x_can_submit'])
        self.assertTrue(
            loan.with_user(self.user_manager).x_can_dm_approve)

    def test_manager_can_still_approve(self):
        """Regression: the DM notification reads the manager's employee."""
        loan = self._own_loan()
        loan.action_submit()
        loan.with_user(self.user_manager).action_dm_approve()
        self.assertEqual(loan.approval_state, 'pending_hr')

    @mute_logger('odoo.addons.base.models.ir_rule')
    def test_employee_cannot_read_stranger_deduction(self):
        stranger = self._make_deduction(self.type_loan,
                                        employee=self.other_emp)
        with self.assertRaises(AccessError):
            stranger.with_user(self.user_employee).read(['name'])

    # ------------------------------------------------------------------
    # 2. The edit window
    # ------------------------------------------------------------------
    def test_gate_field_tracks_the_window(self):
        loan = self._own_loan()
        emp_view = loan.with_user(self.user_employee)
        self.assertTrue(emp_view.x_can_employee_edit, 'draft')
        loan.action_submit()
        emp_view.invalidate_recordset()
        self.assertTrue(emp_view.x_can_employee_edit, 'pending_dm')
        loan.with_user(self.user_manager).action_dm_approve()
        emp_view.invalidate_recordset()
        self.assertFalse(emp_view.x_can_employee_edit, 'pending_hr')

    def test_gate_field_false_for_somebody_elses_request(self):
        loan = self._own_loan()
        loan.action_submit()
        self.assertFalse(
            loan.with_user(self.user_manager).x_can_employee_edit)

    def test_employee_edits_own_draft(self):
        loan = self._own_loan()
        loan.with_user(self.user_employee).write({
            'amount': 900.0, 'installments': 2, 'reason': 'Revised'})
        self.assertEqual(loan.amount, 900.0)

    def test_employee_edits_own_request_pending_dm(self):
        loan = self._own_loan()
        loan.action_submit()
        loan.with_user(self.user_employee).write({'amount': 800.0})
        self.assertEqual(loan.amount, 800.0)

    def test_employee_cannot_edit_after_first_approval(self):
        loan = self._own_loan()
        loan.action_submit()
        loan.with_user(self.user_manager).action_dm_approve()
        with self.assertRaises(UserError):
            loan.with_user(self.user_employee).write({'amount': 800.0})

    def test_employee_cannot_edit_own_penalty(self):
        pen = self._make_deduction(self.type_gov_pen, employee=self.employee)
        pen.action_submit()
        with self.assertRaises(UserError):
            pen.with_user(self.user_employee).write({'amount': 1.0})

    def test_employee_cannot_skip_the_approval_chain(self):
        """Write access on their own draft must not let an employee push
        the request past its approvers over RPC."""
        loan = self._own_loan()
        loan.action_submit()
        for vals in ({'approval_state': 'pending_gm'},
                     {'state': 'active'},
                     {'employee_id': self.other_emp.id},
                     {'dm_approved_by': self.manager_emp.id}):
            with self.assertRaises(UserError):
                loan.with_user(self.user_employee).write(vals)
        self.assertEqual(loan.approval_state, 'pending_dm')
        self.assertEqual(loan.employee_id, self.employee)

    @mute_logger('odoo.addons.base.models.ir_rule')
    def test_employee_cannot_edit_subordinates_request(self):
        """A plain user sees their subordinates' deductions (read rule)
        but the self-service window is their OWN records only."""
        self.employee_b.write({'parent_id': self.employee.id})
        sub = self._make_deduction(self.type_loan, employee=self.employee_b)
        with self.assertRaises(UserError):
            sub.with_user(self.user_employee).write({'amount': 10.0})

    def test_employee_can_still_post_in_the_chatter(self):
        """mail.thread bookkeeping is not a business edit — it must keep
        working outside the window."""
        loan = self._own_loan()
        loan.action_submit()
        loan.with_user(self.user_manager).action_dm_approve()
        loan.with_user(self.user_employee).message_subscribe(
            partner_ids=[self.user_employee.partner_id.id])

    # ------------------------------------------------------------------
    # 3. The delete window
    # ------------------------------------------------------------------
    def test_employee_deletes_own_draft(self):
        loan = self._own_loan()
        loan.with_user(self.user_employee).unlink()

    def test_employee_deletes_own_request_pending_dm(self):
        loan = self._own_loan()
        loan.action_submit()
        loan.with_user(self.user_employee).unlink()

    def test_employee_cannot_delete_after_first_approval(self):
        loan = self._own_loan()
        loan.action_submit()
        loan.with_user(self.user_manager).action_dm_approve()
        with self.assertRaises(UserError):
            loan.with_user(self.user_employee).unlink()

    def test_employee_cannot_delete_own_penalty(self):
        pen = self._make_deduction(self.type_gov_pen, employee=self.employee)
        with self.assertRaises(UserError):
            pen.with_user(self.user_employee).unlink()

    @mute_logger('odoo.addons.base.models.ir_rule')
    def test_employee_cannot_delete_subordinates_request(self):
        self.employee_b.write({'parent_id': self.employee.id})
        sub = self._make_deduction(self.type_loan, employee=self.employee_b)
        with self.assertRaises(UserError):
            sub.with_user(self.user_employee).unlink()

    @mute_logger('odoo.addons.base.models.ir_model')
    def test_employee_still_cannot_create(self):
        """The self-service wizard stays the only route in."""
        with self.assertRaises(AccessError):
            self.env['ksw.deduction'].with_user(self.user_employee).create({
                'employee_id': self.employee.id,
                'type_id': self.type_loan.id,
                'amount': 100.0, 'installments': 1,
            })


class TestSelfServiceRuleSplitRegression(DeductionCommon):
    """The user record rule went read-only so the new write/unlink ACL
    could not leak. Everything that used to ride on it must still work.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)
        cls.supervisor_emp = cls.env['hr.employee'].create({
            'name': 'KSWSELF Supervisor', 'department_id': cls.dept_a.id,
        })
        cls.employee.write({'parent_id': cls.supervisor_emp.id})
        cls.user_supervisor = Users.create({
            'name': 'kswself_sup', 'login': 'kswself_sup',
            'email': 'sup@kswself.test',
            'group_ids': [(6, 0, [
                cls.env.ref('KSW_base_security.group_hr_employee_subordinate').id,
                cls.env.ref('KSW_deduction.group_deduction_supervisor').id,
            ])],
        })
        cls.supervisor_emp.write({'user_id': cls.user_supervisor.id})

    def test_supervisor_creates_and_edits_for_subordinate(self):
        loan = self.env['ksw.deduction'].with_user(self.user_supervisor).create({
            'employee_id': self.employee.id,
            'type_id': self.type_loan.id,
            'amount': 500.0, 'installments': 2,
            'start_month': self.this_month,
        })
        loan.with_user(self.user_supervisor).write({'amount': 600.0})
        self.assertEqual(loan.amount, 600.0)
        loan.with_user(self.user_supervisor).action_submit()
        self.assertEqual(loan.approval_state, 'pending_dm')

    def test_manager_keeps_full_access(self):
        mgr_emp = self.env['hr.employee'].create({
            'name': 'KSWSELF Ded Manager', 'department_id': self.dept_b.id,
        })
        user_manager = self.env['res.users'].with_context(
            no_reset_password=True).create({
                'name': 'kswself_dedmgr', 'login': 'kswself_dedmgr',
                'email': 'dedmgr@kswself.test',
                'group_ids': [(6, 0, [
                    self.env.ref(
                        'KSW_deduction.group_deduction_manager').id])],
            })
        mgr_emp.write({'user_id': user_manager.id})
        ded = self._make_deduction(employee=self.employee)
        ded.with_user(user_manager).write({'reason': 'Manager edit'})
        ded.with_user(user_manager).unlink()
