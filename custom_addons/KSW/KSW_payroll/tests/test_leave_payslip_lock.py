# -*- coding: utf-8 -*-
"""A leave carrying a confirmed payslip counts as finalised.

``hr.leave._is_finalised`` (KSW_annual_leave) closes every reversal route
once a request is approved. KSW_payroll extends it with the money side: if
the vacation payslip has already been confirmed, reversing the leave would
cancel a *paid* slip — the exact damage that cost KSWCO SLIP/11307 a
zero-net July salary when the released installments were re-collected.
"""
from datetime import date, timedelta

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestLeavePayslipLock(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _mkuser(name, login, groups=()):
            return cls.env['res.users'].create({
                'name': name,
                'login': login,
                'email': '%s@leaveslip.test' % login,
                'group_ids': [(6, 0, [cls.env.ref('base.group_user').id]
                               + [cls.env.ref(g).id for g in groups])],
            })

        cls.user_hr = _mkuser('Slip Lock HR', 'leaveslip_hr', (
            'KSW_annual_leave.group_annual_leave_hr',
            'KSW_annual_leave.group_leave_officer',
            'om_hr_payroll.group_hr_payroll_user',
        ))
        cls.user_admin = _mkuser('Slip Lock Admin', 'leaveslip_admin',
                                 ('base.group_system',))
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Slip Lock Employee',
            'user_id': _mkuser('Slip Lock Emp', 'leaveslip_emp').id,
        })
        cls.annual_type = cls.env['hr.leave.type'].create({
            'name': 'Annual Leave Payslip-Lock Test',
            'requires_allocation': False,
            'leave_validation_type': 'annual_multi',
            'is_annual_leave': True,
        })

    def _leave_with_payslip(self, slip_state):
        """A mid-chain request (pending_hr) carrying a payslip."""
        start = date.today() + timedelta(days=20)
        leave = self.env['hr.leave'].sudo().create({
            'employee_id': self.employee.id,
            'holiday_status_id': self.annual_type.id,
            'request_date_from': start,
            'request_date_to': start + timedelta(days=3),
        })
        leave.sudo().write({'x_annual_approval_state': 'pending_hr'})
        slip = self.env['hr.payslip'].sudo().create({
            'employee_id': self.employee.id,
            'name': 'Vacation payslip for leave lock test',
            'date_from': date.today().replace(day=1),
            'date_to': date.today().replace(day=28),
            'x_leave_id': leave.id,
        })
        slip.write({'state': slip_state})
        return leave, slip

    def _leave_with_auto_confirmed_payslip(self):
        """The shape the approval chain now produces: a confirmed payslip
        that was confirmed *by the chain*, not by a payroll officer."""
        leave, slip = self._leave_with_payslip('done')
        slip.write({'x_vacation_auto_confirmed': True})
        return leave, slip

    def test_draft_payslip_leaves_the_request_reversible(self):
        """A provisional calculation must not lock the approval chain."""
        leave, _slip = self._leave_with_payslip('draft')
        self.assertFalse(leave._is_finalised())
        leave.with_user(self.user_hr).action_refuse()
        self.assertEqual(leave.state, 'refuse')

    def test_confirmed_payslip_finalises_the_request(self):
        leave, _slip = self._leave_with_payslip('done')
        self.assertTrue(leave._is_finalised())

    def test_hr_cannot_refuse_a_leave_with_a_confirmed_payslip(self):
        leave, _slip = self._leave_with_payslip('done')
        with self.assertRaises(UserError):
            leave.with_user(self.user_hr).action_refuse()
        self.assertEqual(leave.state, 'confirm')

    def test_hr_cannot_delete_a_leave_with_a_confirmed_payslip(self):
        leave, _slip = self._leave_with_payslip('done')
        with self.assertRaises(UserError):
            leave.with_user(self.user_hr).unlink()
        self.assertTrue(leave.exists())

    # ------------------------------------------------------------------
    # The admin return wizard refuses a request whose slip is already paid
    # ------------------------------------------------------------------

    def _admin_return(self, leave, target):
        step = self.env.ref('KSW_annual_leave.return_step_%s' % target)
        return self.env['ksw.gm.return.approver.wizard'].with_user(
            self.user_admin).create({
                'leave_id': leave.id,
                'target_step_id': step.id,
                'reason': 'Needs a correction',
            }).action_confirm()

    def test_admin_return_blocked_by_a_confirmed_payslip(self):
        """Cancelling a paid slip re-collects its deductions (SLIP/11307)."""
        leave, _slip = self._leave_with_payslip('done')
        with self.assertRaises(UserError):
            self._admin_return(leave, 'pending_dm')
        self.assertEqual(leave.x_annual_approval_state, 'pending_hr')

    def test_admin_return_allowed_with_a_draft_payslip(self):
        """A provisional calculation must not stand in the way."""
        leave, slip = self._leave_with_payslip('draft')
        self._admin_return(leave, 'pending_dm')
        self.assertEqual(leave.x_annual_approval_state, 'pending_dm')
        self.assertEqual(slip.state, 'draft',
                         'a mid-chain return leaves the preview alone')

    def test_admin_return_releases_an_auto_confirmed_payslip(self):
        """The settlement the chain confirmed by itself is undone, not a
        wall: cancelling it puts the deduction installments back to pending
        so a later payroll run collects them."""
        leave, slip = self._leave_with_auto_confirmed_payslip()
        self._admin_return(leave, 'pending_dm')
        self.assertEqual(leave.x_annual_approval_state, 'pending_dm')
        self.assertEqual(slip.state, 'cancel')
        self.assertFalse(leave.sudo().x_vacation_payslip_id)

    def test_auto_confirmed_flag_does_not_unlock_ordinary_reversal(self):
        """Only the admin return wizard releases it. A finalised request is
        still out of an HR user's hands (KSW_annual_leave's lock)."""
        leave, _slip = self._leave_with_auto_confirmed_payslip()
        leave.sudo().write({'x_annual_approval_state': 'approved'})
        with self.assertRaises(UserError):
            leave.with_user(self.user_hr).action_refuse()
