# -*- coding: utf-8 -*-
"""A KSW Supervisor must be able to refuse and delete a subordinate's
annual leave request — including one they raised themselves by mistake.

Regression (July 2026, prod KSWCO): a supervisor with only
``group_leave_supervisor_cascading`` hit

    AccessError: You do not have enough rights to access the field
    "x_vacation_payslip_ids" on Time Off (hr.leave)

when pressing **Refuse** on a subordinate's annual leave sitting at
``pending_gm_initial``. The refuse itself succeeded; what blew up was the
KSW_payroll post-processing (``_cancel_vacation_payslips``), which reads
the payroll-group-restricted ``x_vacation_payslip_ids`` /
``x_vacation_payslip_id`` fields as the calling user. The AccessError
rolled the whole transaction back, so the leave stayed pending.

Deleting the refused leave afterwards was blocked too: the KSW manager
branch of ``_unlink_if_correct_states`` only allowed
confirm/validate1/cancel, so a refused request could only be removed by a
Time-Off Administrator.
"""
from datetime import date, datetime, timedelta

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase


class TestSupervisorRefuseDelete(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _mkuser(name, login, groups):
            return cls.env['res.users'].create({
                'name': name,
                'login': login,
                'email': '%s@suprefdel.test' % login,
                'group_ids': [(6, 0, [g.id for g in groups])],
            })

        cls.group_user = cls.env.ref('base.group_user')

        # The supervisor: KSW "Supervisor Cascading" tier only — no payroll,
        # no HR, no Time-Off Officer. This is the real prod profile.
        cls.user_sup = _mkuser('Sup Refuse Del', 'suprefdel_sup', [
            cls.group_user,
            cls.env.ref('KSW_annual_leave.group_leave_supervisor_cascading'),
        ])
        cls.emp_sup = cls.env['hr.employee'].create({
            'name': 'Sup Refuse Del Employee',
            'user_id': cls.user_sup.id,
        })

        # HR approver — only used to walk the chain one step forward.
        cls.user_hr = _mkuser('HR Refuse Del', 'suprefdel_hr', [
            cls.group_user,
            cls.env.ref('KSW_annual_leave.group_annual_leave_hr'),
        ])
        cls.env['hr.employee'].create({
            'name': 'HR Refuse Del Employee', 'user_id': cls.user_hr.id})

        # The subordinate — reports to the supervisor through parent_id
        # (the KSW record rules) *and* leave_manager_id (Odoo's own
        # approval check in _check_approval_update).
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Sub Refuse Del Employee',
            'parent_id': cls.emp_sup.id,
            'leave_manager_id': cls.user_sup.id,
        })
        # A wage + salary structure so a provisional vacation payslip can
        # actually be produced (needed by the payslip-cancellation test).
        cls.employee.current_version_id.write({
            'date_version': date(2025, 1, 1),
            'contract_date_start': date(2025, 1, 1),
            'wage': 6000.0,
            'struct_id': cls.env.ref('om_hr_payroll.structure_base').id,
        })
        cls.employee._compute_current_version_id()

        cls.leave_type = cls.env['hr.leave.type'].create({
            'name': 'Annual Leave Supervisor Refuse Test',
            'requires_allocation': False,
            'leave_validation_type': 'annual_multi',
            'is_annual_leave': True,
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_leave(self, approval_state='pending_dm'):
        """Insert an annual_multi request straight in SQL.

        Same approach as test_annual_multi_approval: it side-steps the
        allocation/attendance create-time machinery, which is not what
        these tests are about.
        """
        date_from = date.today() + timedelta(days=3)
        date_to = date_from + timedelta(days=10)
        cal_days = (date_to - date_from).days + 1
        self.env.cr.execute("""
            INSERT INTO hr_leave
                (employee_id, holiday_status_id, company_id, state,
                 request_date_from, request_date_to, date_from, date_to,
                 number_of_days, number_of_hours,
                 x_return_state, x_annual_approval_state,
                 create_uid, write_uid, create_date, write_date)
            VALUES (%s, %s, %s, 'confirm', %s, %s, %s, %s, %s, %s,
                    'not_applicable', %s, %s, %s, NOW(), NOW())
            RETURNING id
        """, (
            self.employee.id, self.leave_type.id, self.env.company.id,
            date_from, date_to,
            datetime.combine(date_from, datetime.min.time()) + timedelta(hours=5),
            datetime.combine(date_to, datetime.min.time()) + timedelta(hours=13, minutes=30),
            cal_days, cal_days * 8.0,
            approval_state, self.user_sup.id, self.user_sup.id,
        ))
        leave_id = self.env.cr.fetchone()[0]
        self.env.invalidate_all()
        return self.env['hr.leave'].browse(leave_id)

    # ==================================================================
    # Refuse
    # ==================================================================

    def test_supervisor_can_refuse_subordinate_leave(self):
        """The reported bug: Refuse must not die on the payslip fields."""
        leave = self._create_leave(approval_state='pending_gm_initial')

        leave.with_user(self.user_sup).action_refuse()

        self.assertEqual(leave.state, 'refuse')
        # action_refuse resets the multi-step chain
        self.assertFalse(leave.x_annual_approval_state)

    def test_refuse_still_cancels_the_vacation_payslip(self):
        """sudo() must not cost us the payslip cleanup."""
        leave = self._create_leave(approval_state='pending_gm_initial')
        leave.with_user(self.user_hr).sudo().action_preview_vacation_payslip()
        payslips = leave.sudo().x_vacation_payslip_ids
        self.assertTrue(
            payslips, 'precondition: a provisional payslip must exist')

        leave.with_user(self.user_sup).action_refuse()

        self.assertEqual(leave.state, 'refuse')
        self.assertEqual(set(payslips.mapped('state')), {'cancel'})
        self.assertFalse(leave.sudo().x_vacation_payslip_id)

    def test_supervisor_cannot_refuse_outside_own_scope(self):
        """The relaxation is scope-bound, not a blanket refuse right."""
        outsider = self.env['hr.employee'].create({
            'name': 'Outsider Refuse Del Employee'})
        leave = self._create_leave()
        leave.sudo().write({'employee_id': outsider.id})

        with self.assertRaises(AccessError):
            leave.with_user(self.user_sup).action_refuse()

    # ==================================================================
    # Delete
    # ==================================================================

    def test_supervisor_can_delete_pending_subordinate_leave(self):
        """Mid-chain request the supervisor raised himself."""
        leave = self._create_leave(approval_state='pending_gm_initial')
        leave.with_user(self.user_sup).unlink()
        self.assertFalse(leave.exists())

    def test_supervisor_can_delete_refused_subordinate_leave(self):
        """Refuse first, then clear the record away."""
        leave = self._create_leave(approval_state='pending_gm_initial')
        leave.with_user(self.user_sup).action_refuse()
        self.assertEqual(leave.state, 'refuse')

        leave.with_user(self.user_sup).unlink()
        self.assertFalse(leave.exists())

    def test_supervisor_still_blocked_on_validated_leave(self):
        """An approved leave stays Administrator-only."""
        leave = self._create_leave()
        leave.sudo().write({'state': 'validate'})
        with self.assertRaises(UserError):
            leave.with_user(self.user_sup).unlink()
        self.assertTrue(leave.exists())

    def test_supervisor_cannot_delete_outside_own_scope(self):
        """Deletion stays inside the supervisor's subtree."""
        outsider = self.env['hr.employee'].create({
            'name': 'Outsider Delete Refuse Employee'})
        leave = self._create_leave()
        leave.sudo().write({'employee_id': outsider.id, 'state': 'refuse'})

        with self.assertRaises(AccessError):
            leave.with_user(self.user_sup).unlink()
        self.assertTrue(leave.exists())
