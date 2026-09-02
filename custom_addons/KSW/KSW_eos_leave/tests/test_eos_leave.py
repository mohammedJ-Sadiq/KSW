"""Tests for the KSW End of Service Leave module.

Covers:
  - Leave creation starts in pending_dm, x_is_eos_leave=True
  - No allocation balance required for EOS approval chain
  - Unpaid days deduction reduces service period before Art. 84/85 recompute
  - Termination reason selects correct EOS formula (Art. 84 vs Art. 85)
  - Full 6-step approval chain completes successfully
  - EOS payslip is created at GM Final Approve with correct inputs
  - EOS payslip is cancelled on refuse / reset-to-draft
  - DM approval does not return the attendance-sheet wizard for EOS
  - HR field write guard raises UserError for non-HR users
"""
import base64
from datetime import date, timedelta

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase


class TestEosLeave(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _mkuser(name, login, extra_groups=()):
            group_ids = [cls.env.ref('base.group_user').id]
            group_ids.extend(extra_groups)
            return cls.env['res.users'].create({
                'name': name,
                'login': login,
                'email': f'{login}@eos.test',
                'group_ids': [(6, 0, group_ids)],
            })

        cls.user_dm  = _mkuser('EOS DM',  'eos_dm')
        cls.user_hr  = _mkuser('EOS HR',  'eos_hr',
                               [cls.env.ref('KSW_annual_leave.group_annual_leave_hr').id])
        cls.user_acc = _mkuser('EOS Acc', 'eos_acc',
                               [cls.env.ref('KSW_annual_leave.group_annual_leave_acc').id])
        cls.user_gm  = _mkuser('EOS GM',  'eos_gm',
                               [cls.env.ref('KSW_annual_leave.group_annual_leave_gm').id])
        cls.user_emp = _mkuser('EOS Emp', 'eos_emp')

        cls.employee = cls.env['hr.employee'].create({
            'name': 'EOS Test Employee',
            'leave_manager_id': cls.user_dm.id,
            'user_id': cls.user_emp.id,
        })

        # hr.employee.create() auto-creates an hr.version with date_version=today.
        # Write the test contract data onto it; no second version is created so
        # current_version_id stays correct (the only version is current).
        cls.structure = cls.env.ref('om_hr_payroll.structure_base')
        default_calendar = cls.env['resource.calendar'].search([], limit=1)
        joining = date(2020, 1, 1)
        cls.version = cls.employee.current_version_id
        cls.version.write({
            'date_version': joining,
            'contract_date_start': joining,
            'wage': 6000.0,
            'struct_id': cls.structure.id,
            'resource_calendar_id': default_calendar.id,
        })
        cls.employee._compute_current_version_id()

        cls.eos_leave_type = cls.env['hr.leave.type'].create({
            'name': 'End of Service Test',
            'leave_validation_type': 'annual_multi',
            'requires_allocation': False,
            'is_eos_leave': True,
            'is_annual_leave': False,
            'time_type': 'leave',
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    _STATE_RANK = {
        'pending_dm': 0,
        'pending_hr': 1,
        'pending_gm_initial': 2,
        'pending_acc': 3,
        'pending_gm_final': 4,
        'pending_employee_signature': 5,
        'approved': 6,
    }

    def _make_leave(self, offset=0):
        """Create an EOS leave request for cls.employee."""
        base = date(2027, 6, 1) + timedelta(days=offset * 30)
        return self.env['hr.leave'].sudo().create({
            'employee_id': self.employee.id,
            'holiday_status_id': self.eos_leave_type.id,
            'request_date_from': base,
            'request_date_to': base,
        })

    def _advance_to(self, leave, target_state):
        """Advance the approval chain up to target_state (resume-aware)."""
        steps = [
            ('pending_dm',         'action_dm_approve',         self.user_dm),
            ('pending_hr',         'action_hr_approve',         self.user_hr),
            ('pending_gm_initial', 'action_gm_initial_approve', self.user_gm),
            ('pending_acc',        'action_acc_approve',        self.user_acc),
            ('pending_gm_final',   'action_gm_final_approve',   self.user_gm),
        ]
        for pre_state, method, user in steps:
            if self._STATE_RANK.get(leave.x_annual_approval_state, 0) > \
                    self._STATE_RANK.get(pre_state, 0):
                continue  # already past this step
            getattr(leave.with_user(user).sudo(), method)()
            if leave.x_annual_approval_state == target_state:
                break

    def _add_stub_attachment(self, leave):
        """Attach a stub PDF (required by action_employee_confirm_signature)."""
        att = self.env['ir.attachment'].sudo().create({
            'name': 'eos_signed_form.pdf',
            'datas': base64.b64encode(b'stub'),
            'res_model': 'hr.leave',
            'res_id': leave.id,
        })
        leave.sudo().write({'x_attachment_ids': [(4, att.id)]})

    # ==================================================================
    # Leave creation
    # ==================================================================

    def test_eos_leave_creation(self):
        leave = self._make_leave()
        self.assertEqual(leave.x_annual_approval_state, 'pending_dm')
        self.assertTrue(leave.x_is_eos_leave)

    def test_eos_leave_type_flag(self):
        self.assertTrue(self.eos_leave_type.is_eos_leave)
        self.assertFalse(self.eos_leave_type.is_annual_leave)

    # ==================================================================
    # No allocation check
    # ==================================================================

    def test_no_allocation_required(self):
        """EOS leave approval must not raise for missing allocation balance."""
        leave = self._make_leave(offset=1)
        # DM approve should succeed without any allocation record
        leave.with_user(self.user_dm).sudo().action_dm_approve()
        self.assertEqual(leave.x_annual_approval_state, 'pending_hr')

    # ==================================================================
    # Adjusted service years computation
    # ==================================================================

    def test_adjusted_service_years_no_unpaid(self):
        """0 unpaid days — adjusted years equal the base EOS service years."""
        leave = self._make_leave(offset=2)
        leave.sudo().write({'x_eos_unpaid_days': 0.0})
        # Base years: same formula as _compute_eos_adjusted, which measures
        # service up to the EOS date (request_date_from) — NOT up to today.
        # Measuring to today made this test drift out of date and fail.
        total_days = max(
            (leave.request_date_from - date(2020, 1, 1)).days, 0)
        expected_years = total_days / 365.25
        self.assertAlmostEqual(
            leave.x_eos_adjusted_service_years, expected_years, places=1)
        # Adjusted termination amount equals base termination amount
        self.assertAlmostEqual(
            leave.x_eos_adjusted_termination_amount,
            leave.x_eos_termination_amount,
            places=2,
        )

    def test_adjusted_service_years_with_unpaid(self):
        """Unpaid days reduce the service period and thus the EOS amounts."""
        leave = self._make_leave(offset=3)
        base_years = leave.x_eos_adjusted_service_years  # unpaid=0
        leave.sudo().write({'x_eos_unpaid_days': 365.0})  # subtract 1 year
        adjusted_years = leave.x_eos_adjusted_service_years
        self.assertLess(adjusted_years, base_years)
        self.assertAlmostEqual(adjusted_years, base_years - 1.0, delta=0.05)

    def test_unpaid_days_reduce_art84_amount(self):
        """Deducting unpaid days decreases the Art. 84 termination amount."""
        leave = self._make_leave(offset=4)
        base_term = leave.x_eos_adjusted_termination_amount
        leave.sudo().write({'x_eos_unpaid_days': 365.0})
        self.assertLess(leave.x_eos_adjusted_termination_amount, base_term)

    # ==================================================================
    # Termination reason → payout selection
    # ==================================================================

    def test_termination_reason_84(self):
        leave = self._make_leave(offset=5)
        leave.sudo().write({'x_eos_termination_reason': '84'})
        self.assertAlmostEqual(
            leave.x_eos_payout_amount,
            leave.x_eos_adjusted_termination_amount,
            places=2,
        )

    def test_termination_reason_85(self):
        leave = self._make_leave(offset=6)
        leave.sudo().write({'x_eos_termination_reason': '85'})
        self.assertAlmostEqual(
            leave.x_eos_payout_amount,
            leave.x_eos_adjusted_resignation_amount,
            places=2,
        )

    def test_no_reason_gives_zero_payout(self):
        leave = self._make_leave(offset=7)
        leave.sudo().write({'x_eos_termination_reason': False})
        self.assertEqual(leave.x_eos_payout_amount, 0.0)

    # ==================================================================
    # Art. 85 resignation tiers
    # ==================================================================

    def test_resignation_zero_under_2_years(self):
        """Employee with <2 years service gets Art. 85 = 0."""
        # Create a fresh employee who joined less than 2 years ago
        emp2 = self.env['hr.employee'].create({'name': 'New Emp EOS'})
        from odoo import fields as ofields
        recent = ofields.Date.context_today(emp2) - timedelta(days=300)
        default_calendar = self.env['resource.calendar'].search([], limit=1)
        v2 = emp2.current_version_id
        v2.write({
            'date_version': recent,
            'contract_date_start': recent,
            'wage': 5000.0,
            'struct_id': self.structure.id,
            'resource_calendar_id': default_calendar.id,
        })
        emp2._compute_current_version_id()
        leave = self.env['hr.leave'].sudo().create({
            'employee_id': emp2.id,
            'holiday_status_id': self.eos_leave_type.id,
            'request_date_from': date(2027, 6, 1),
            'request_date_to': date(2027, 6, 1),
        })
        leave.sudo().write({'x_eos_termination_reason': '85'})
        self.assertEqual(leave.x_eos_payout_amount, 0.0)

    # ==================================================================
    # Full approval chain
    # ==================================================================

    def test_full_approval_chain(self):
        """All 6 steps complete; leave reaches state='validate'."""
        leave = self._make_leave(offset=8)
        # Required from the HR step onwards — the chain will not advance past
        # pending_dm without it, sudo included.
        leave.sudo().write({'x_eos_termination_reason': '84'})
        self._advance_to(leave, 'pending_employee_signature')
        self.assertEqual(leave.x_annual_approval_state, 'pending_employee_signature')
        self._add_stub_attachment(leave)
        leave.with_user(self.user_emp).sudo().action_employee_confirm_signature()
        self.assertEqual(leave.x_annual_approval_state, 'approved')
        self.assertEqual(leave.state, 'validate')

    # ==================================================================
    # Employee archival at GM Final Approval
    # ==================================================================

    def test_employee_archived_on_gm_final_art84(self):
        """GM Final Approve on Art. 84 EOS archives the employee as 'Fired'."""
        leave = self._make_leave(offset=17)
        leave.sudo().write({'x_eos_termination_reason': '84'})
        self._advance_to(leave, 'pending_employee_signature')
        emp = leave.employee_id.with_context(active_test=False)
        self.assertFalse(emp.active)
        self.assertEqual(
            emp.departure_reason_id,
            self.env.ref('hr.departure_fired'),
        )
        self.assertEqual(emp.departure_date, leave.request_date_from)

    def test_employee_archived_on_gm_final_art85(self):
        """GM Final Approve on Art. 85 EOS archives the employee as 'Resigned'."""
        leave = self._make_leave(offset=18)
        leave.sudo().write({'x_eos_termination_reason': '85'})
        self._advance_to(leave, 'pending_employee_signature')
        emp = leave.employee_id.with_context(active_test=False)
        self.assertFalse(emp.active)
        self.assertEqual(
            emp.departure_reason_id,
            self.env.ref('hr.departure_resigned'),
        )

    def test_employee_not_archived_for_non_eos_leave(self):
        """GM Final Approve on a regular annual leave must NOT archive the employee."""
        annual_type = self.env['hr.leave.type'].create({
            'name': 'Annual Leave Archive Guard Test',
            'leave_validation_type': 'annual_multi',
            'requires_allocation': False,
            'is_eos_leave': False,
            'is_annual_leave': True,
        })
        leave = self.env['hr.leave'].sudo().create({
            'employee_id': self.employee.id,
            'holiday_status_id': annual_type.id,
            'request_date_from': date(2027, 9, 1),
            'request_date_to': date(2027, 9, 5),
        })
        self._advance_to(leave, 'pending_employee_signature')
        self.assertTrue(self.employee.active)

    # ==================================================================
    # EOS payslip creation
    # ==================================================================

    def test_eos_payslip_created_at_gm_final(self):
        """After GM Final Approve, x_eos_payslip_id must be set."""
        leave = self._make_leave(offset=9)
        leave.sudo().write({
            'x_eos_unpaid_days': 30.0,
            'x_eos_termination_reason': '84',
            'x_eos_previous_payments': 500.0,
            'x_eos_notice_pay': 200.0,
        })
        self._advance_to(leave, 'pending_employee_signature')
        self.assertTrue(leave.x_eos_payslip_id)

    def test_payslip_inputs_correct(self):
        """EOS payslip inputs match the leave field values."""
        leave = self._make_leave(offset=10)
        leave.sudo().write({
            'x_eos_unpaid_days': 0.0,
            'x_eos_termination_reason': '84',
            'x_eos_previous_payments': 1000.0,
            'x_eos_notice_pay': 300.0,
        })
        self._advance_to(leave, 'pending_employee_signature')

        payslip = leave.x_eos_payslip_id
        self.assertTrue(payslip)

        input_by_code = {inp.code: inp for inp in payslip.input_line_ids}
        self.assertIn('EOS_AMOUNT', input_by_code)
        self.assertAlmostEqual(
            input_by_code['EOS_AMOUNT'].amount,
            leave.x_eos_payout_amount,
            places=2,
        )
        self.assertIn('EOS_PREV_PAYMENTS', input_by_code)
        self.assertAlmostEqual(
            input_by_code['EOS_PREV_PAYMENTS'].amount, 1000.0, places=2)
        self.assertIn('EOS_NOTICE_PAY', input_by_code)
        self.assertAlmostEqual(
            input_by_code['EOS_NOTICE_PAY'].amount, 300.0, places=2)

    def test_no_eos_payslip_on_vacation_leave(self):
        """A regular annual leave (non-EOS) must not get an EOS payslip."""
        annual_type = self.env['hr.leave.type'].create({
            'name': 'Annual Leave EOS Test Guard',
            'leave_validation_type': 'annual_multi',
            'requires_allocation': False,
            'is_eos_leave': False,
            'is_annual_leave': True,
        })
        leave = self.env['hr.leave'].sudo().create({
            'employee_id': self.employee.id,
            'holiday_status_id': annual_type.id,
            'request_date_from': date(2027, 8, 1),
            'request_date_to': date(2027, 8, 5),
        })
        self._advance_to(leave, 'pending_employee_signature')
        # No EOS payslip on a regular annual leave
        self.assertFalse(leave.x_eos_payslip_id)

    # ==================================================================
    # Payslip cancellation on refuse / draft
    # ==================================================================

    def test_eos_payslip_cancelled_on_refuse(self):
        """Refusing the leave must cancel the EOS payslip."""
        leave = self._make_leave(offset=11)
        leave.sudo().write({'x_eos_termination_reason': '84'})
        self._advance_to(leave, 'pending_employee_signature')

        payslip = leave.x_eos_payslip_id
        self.assertTrue(payslip)

        leave.sudo().action_refuse()
        self.assertEqual(payslip.state, 'cancel')
        self.assertFalse(leave.x_eos_payslip_id)

    def test_eos_payslip_cancelled_on_draft(self):
        """Resetting to draft from pending-signature cancels the EOS payslip.

        At pending_employee_signature the Odoo state is still 'confirm'
        (validate only happens after action_employee_confirm_signature), so
        action_draft() is allowed and must clear the payslip.
        """
        leave = self._make_leave(offset=12)
        leave.sudo().write({'x_eos_termination_reason': '84'})
        self._advance_to(leave, 'pending_employee_signature')
        self.assertEqual(leave.x_annual_approval_state, 'pending_employee_signature')
        self.assertEqual(leave.state, 'confirm')  # not yet validated

        payslip = leave.x_eos_payslip_id
        self.assertTrue(payslip)

        leave.sudo().action_draft()
        self.assertEqual(payslip.state, 'cancel')
        self.assertFalse(leave.x_eos_payslip_id)

    # ==================================================================
    # DM step: no attendance wizard
    # ==================================================================

    def test_dm_approve_no_wizard(self):
        """DM approve on an EOS leave must not return the attendance wizard."""
        leave = self._make_leave(offset=13)
        result = leave.with_user(self.user_dm).sudo().action_dm_approve()
        # Result should be None/True (not a dict with res_model wizard)
        if isinstance(result, dict):
            self.assertNotEqual(
                result.get('res_model'), 'ksw.leave.attendance.wizard',
                'EOS DM approval should not open the attendance wizard.',
            )
        self.assertEqual(leave.x_annual_approval_state, 'pending_hr')

    # ==================================================================
    # HR field write guard
    # ==================================================================

    def test_hr_field_write_guard_non_hr(self):
        """A DM user writing x_eos_unpaid_days must raise UserError."""
        leave = self._make_leave(offset=14)
        with self.assertRaises(UserError):
            leave.with_user(self.user_dm).write({'x_eos_unpaid_days': 10.0})

    def test_hr_field_write_allowed_for_hr(self):
        """An HR user can write x_eos_unpaid_days without error."""
        leave = self._make_leave(offset=15)
        leave.with_user(self.user_hr).sudo().write({'x_eos_unpaid_days': 10.0})
        self.assertAlmostEqual(leave.x_eos_unpaid_days, 10.0, places=2)

    def test_hr_field_write_allowed_sudo(self):
        """sudo() bypasses the HR write guard."""
        leave = self._make_leave(offset=16)
        leave.sudo().write({'x_eos_previous_payments': 500.0})
        self.assertAlmostEqual(leave.x_eos_previous_payments, 500.0, places=2)

    def test_hr_field_clear_allowed_for_non_hr(self):
        """Clearing an EOS figure to 0/False is open to every role.

        Only *setting* a meaningful value is HR-gated — otherwise a chain
        reset triggered by a non-HR user (a GM refusing, say) would raise.
        """
        leave = self._make_leave(offset=17)
        leave.sudo().write({'x_eos_unpaid_days': 10.0})

        leave.with_user(self.user_dm).sudo().write({'x_eos_unpaid_days': 0.0})

        self.assertAlmostEqual(leave.x_eos_unpaid_days, 0.0, places=2)

    # ==================================================================
    # Chain reset clears the HR-filled EOS figures
    # ==================================================================

    def test_refuse_clears_eos_financial_fields(self):
        """Refusing wipes the EOS figures along with the rest of the chain."""
        leave = self._make_leave(offset=18)
        leave.sudo().write({
            'x_eos_termination_reason': '84',
            'x_eos_unpaid_days': 10.0,
            'x_eos_previous_payments': 500.0,
            'x_eos_notice_pay': 250.0,
        })
        self._advance_to(leave, 'pending_gm_final')

        leave.sudo().action_refuse()

        self.assertFalse(leave.x_eos_termination_reason)
        self.assertAlmostEqual(leave.x_eos_unpaid_days, 0.0, places=2)
        self.assertAlmostEqual(leave.x_eos_previous_payments, 0.0, places=2)
        self.assertAlmostEqual(leave.x_eos_notice_pay, 0.0, places=2)
        # The payout derives from the termination reason, so it clears too.
        self.assertAlmostEqual(leave.x_eos_payout_amount, 0.0, places=2)

    def test_draft_clears_eos_financial_fields(self):
        """Back-to-draft wipes the EOS figures so the chain restarts clean."""
        leave = self._make_leave(offset=19)
        leave.sudo().write({
            'x_eos_termination_reason': '85',
            'x_eos_previous_payments': 750.0,
        })
        self._advance_to(leave, 'pending_gm_final')

        leave.sudo().action_draft()

        self.assertFalse(leave.x_eos_termination_reason)
        self.assertAlmostEqual(leave.x_eos_previous_payments, 0.0, places=2)

    # ==================================================================
    # Single-day date sync on write (not just on create)
    # ==================================================================

    def test_type_change_to_eos_syncs_end_date(self):
        """Switching an existing multi-day leave to an EOS type collapses it."""
        other_type = self.env['hr.leave.type'].create({
            'name': 'Non-EOS Switch Source',
            'leave_validation_type': 'annual_multi',
            'requires_allocation': False,
            'is_eos_leave': False,
        })
        start = date(2027, 12, 1)
        leave = self.env['hr.leave'].sudo().create({
            'employee_id': self.employee.id,
            'holiday_status_id': other_type.id,
            'request_date_from': start,
            'request_date_to': start + timedelta(days=5),
        })
        self.assertNotEqual(leave.request_date_from, leave.request_date_to)

        leave.sudo().write({'holiday_status_id': self.eos_leave_type.id})

        self.assertEqual(leave.request_date_to, leave.request_date_from)

    def test_moving_start_date_syncs_end_date(self):
        """Editing request_date_from on an EOS leave drags the end date along."""
        leave = self._make_leave(offset=20)
        new_start = leave.request_date_from + timedelta(days=3)

        leave.sudo().write({'request_date_from': new_start})

        self.assertEqual(leave.request_date_from, new_start)
        self.assertEqual(leave.request_date_to, new_start)

    # ==================================================================
    # Termination Reason guard (missing reason ⇒ no EOS_AMOUNT on payslip)
    # ==================================================================

    def test_hr_approve_blocked_without_termination_reason(self):
        """HR cannot approve an EOS request that has no Termination Reason.

        Without the reason x_eos_payout_amount is 0 and no EOS_AMOUNT input
        is written, so the EOS payslip would silently omit the payout.
        """
        leave = self._make_leave(offset=21)
        leave.with_user(self.user_dm).sudo().action_dm_approve()
        self.assertEqual(leave.x_annual_approval_state, 'pending_hr')
        self.assertFalse(leave.x_eos_termination_reason)

        with self.assertRaises(UserError) as ctx:
            leave.with_user(self.user_hr).action_hr_approve()
        self.assertIn('Termination Reason', str(ctx.exception))

        self.assertEqual(leave.x_annual_approval_state, 'pending_hr')

    def test_sudo_cannot_bypass_termination_reason(self):
        """Data-integrity guard, not a permission one — sudo is no escape."""
        leave = self._make_leave(offset=28)
        leave.with_user(self.user_dm).sudo().action_dm_approve()

        with self.assertRaises(UserError) as ctx:
            leave.with_user(self.user_hr).sudo().action_hr_approve()
        self.assertIn('Termination Reason', str(ctx.exception))

    def test_dm_step_exempt_from_termination_reason(self):
        """The DM approves before HR has ever seen the request."""
        leave = self._make_leave(offset=29)
        self.assertFalse(leave.x_eos_termination_reason)

        leave.with_user(self.user_dm).sudo().action_dm_approve()

        self.assertEqual(leave.x_annual_approval_state, 'pending_hr')

    def test_later_steps_blocked_without_termination_reason(self):
        """A reason cleared mid-chain stops every remaining forward step.

        Covers the GM's return-to-approver path, which re-enters the chain at
        Accounting and so never passes back through the HR step.
        """
        leave = self._make_leave(offset=30)
        leave.sudo().write({'x_eos_termination_reason': '84'})
        self._advance_to(leave, 'pending_acc')
        self.assertEqual(leave.x_annual_approval_state, 'pending_acc')

        leave.sudo().write({'x_eos_termination_reason': False})

        with self.assertRaises(UserError) as ctx:
            leave.with_user(self.user_acc).action_acc_approve()
        self.assertIn('Termination Reason', str(ctx.exception))
        self.assertEqual(leave.x_annual_approval_state, 'pending_acc')

    def test_hr_approve_allowed_once_reason_filled(self):
        """Filling the reason unblocks the same HR approval."""
        leave = self._make_leave(offset=22)
        leave.with_user(self.user_dm).sudo().action_dm_approve()
        leave.with_user(self.user_hr).write({'x_eos_termination_reason': '84'})

        leave.with_user(self.user_hr).action_hr_approve()

        self.assertEqual(leave.x_annual_approval_state, 'pending_gm_initial')

    def test_definitive_payslip_refuses_missing_reason(self):
        """A non-preview EOS payslip is never generated without a reason."""
        leave = self._make_leave(offset=23)
        with self.assertRaises(UserError) as ctx:
            leave.with_user(self.user_hr)._create_eos_payslip(preview=False)
        self.assertIn('Termination Reason', str(ctx.exception))

    def test_preview_payslip_allowed_without_reason(self):
        """A provisional run may legitimately predate the HR figures.

        Only the guard is under test — payroll access on hr.version.wage is
        out of scope, so an AccessError further down is tolerated while a
        UserError (the guard) is not.
        """
        leave = self._make_leave(offset=24)
        try:
            leave.with_user(self.user_hr)._create_eos_payslip(preview=True)
        except AccessError:
            pass  # AccessError subclasses UserError — must be caught first
        except UserError:
            self.fail('A provisional EOS payslip must not require a reason.')

    def test_preview_payslip_has_no_eos_amount_without_reason(self):
        """The provisional payslip simply carries no EOS_AMOUNT line."""
        leave = self._make_leave(offset=27)
        leave.sudo()._create_eos_payslip(preview=True)
        self.assertTrue(leave.x_eos_payslip_id)
        self.assertFalse(
            leave.sudo().x_eos_payslip_id.input_line_ids.filtered(
                lambda i: i.code == 'EOS_AMOUNT'))

    # ==================================================================
    # HR may still correct the EOS inputs after step 2
    # ==================================================================

    def test_eos_inputs_editable_only_at_hr_step(self):
        """HR owns these figures at their own step and nowhere else."""
        leave = self._make_leave(offset=25)
        leave.with_user(self.user_dm).sudo().action_dm_approve()
        self.assertEqual(leave.x_annual_approval_state, 'pending_hr')

        self.assertTrue(leave.with_user(self.user_hr).x_eos_inputs_editable)
        self.assertFalse(leave.with_user(self.user_acc).x_eos_inputs_editable)

        leave.with_user(self.user_hr).write({'x_eos_previous_payments': 500.0})
        self.assertEqual(leave.x_eos_previous_payments, 500.0)

    def test_eos_inputs_frozen_once_past_hr_step(self):
        """Past their step HR can no longer rewrite the figures — by RPC either.

        The correction path is the GM's return-to-approver wizard, which
        accepts 'pending_hr' as a target from both GM steps.
        """
        leave = self._make_leave(offset=31)
        leave.sudo().write({'x_eos_termination_reason': '84'})
        self._advance_to(leave, 'pending_gm_final')

        self.assertFalse(leave.with_user(self.user_hr).x_eos_inputs_editable)

        with self.assertRaises(UserError) as ctx:
            leave.with_user(self.user_hr).write(
                {'x_eos_previous_payments': 500.0})
        self.assertIn('HR Approval step', str(ctx.exception))
        self.assertFalse(leave.x_eos_previous_payments)

    def test_gm_return_reopens_hr_edit_window(self):
        """Returning the request to HR makes the figures writable again."""
        leave = self._make_leave(offset=32)
        leave.sudo().write({'x_eos_termination_reason': '84'})
        self._advance_to(leave, 'pending_gm_final')

        wizard = self.env['ksw.gm.return.approver.wizard'].with_user(
            self.user_gm).sudo().create({
                'leave_id': leave.id,
                'target_step_id': self.env.ref(
                    'KSW_annual_leave.return_step_pending_hr').id,
                'reason': 'Wrong termination reason — please correct.',
            })
        wizard.action_confirm()

        self.assertEqual(leave.x_annual_approval_state, 'pending_hr')
        self.assertTrue(leave.with_user(self.user_hr).x_eos_inputs_editable)
        leave.with_user(self.user_hr).write({'x_eos_termination_reason': '85'})
        self.assertEqual(leave.x_eos_termination_reason, '85')

    def test_eos_inputs_not_editable_once_approved(self):
        """The gate closes when the request leaves the approval chain."""
        leave = self._make_leave(offset=26)
        leave.sudo().write({'x_eos_termination_reason': '84'})
        self._advance_to(leave, 'pending_employee_signature')
        self._add_stub_attachment(leave)
        leave.with_user(self.user_hr).sudo().action_employee_confirm_signature()

        self.assertEqual(leave.x_annual_approval_state, 'approved')
        self.assertFalse(leave.with_user(self.user_hr).x_eos_inputs_editable)

    # ==================================================================
    # Employee restoration when GM final approval is undone
    # ==================================================================

    def test_archive_is_stamped_on_the_request(self):
        """The request records that it is what archived the employee.

        Nothing else makes the restoration below safe: 'the employee is
        inactive' is also true of everyone HR archived for their own reasons.
        """
        leave = self._make_leave(offset=27)
        leave.sudo().write({'x_eos_termination_reason': '84'})
        self._advance_to(leave, 'pending_employee_signature')
        self.assertTrue(leave.x_eos_employee_archived)

    def test_refuse_restores_the_employee(self):
        leave = self._make_leave(offset=28)
        leave.sudo().write({'x_eos_termination_reason': '84'})
        self._advance_to(leave, 'pending_employee_signature')
        emp = leave.employee_id.with_context(active_test=False)
        self.assertFalse(emp.active)

        leave.sudo().action_refuse()

        self.assertTrue(emp.active)
        self.assertFalse(emp.departure_reason_id)
        self.assertFalse(emp.departure_date)
        self.assertFalse(leave.x_eos_employee_archived)

    def test_draft_restores_the_employee(self):
        leave = self._make_leave(offset=29)
        leave.sudo().write({'x_eos_termination_reason': '85'})
        self._advance_to(leave, 'pending_employee_signature')
        leave.sudo().action_draft()
        self.assertTrue(leave.employee_id.with_context(active_test=False).active)

    def test_cancel_restores_the_employee(self):
        leave = self._make_leave(offset=30)
        leave.sudo().write({'x_eos_termination_reason': '84'})
        self._advance_to(leave, 'pending_employee_signature')
        leave.sudo()._action_user_cancel('Termination called off.')
        self.assertTrue(leave.employee_id.with_context(active_test=False).active)

    def test_gm_return_restores_the_employee(self):
        """Sent back for revision: the employee is on staff again meanwhile."""
        leave = self._make_leave(offset=31)
        leave.sudo().write({'x_eos_termination_reason': '84'})
        self._advance_to(leave, 'pending_employee_signature')
        wizard = self.env['ksw.gm.return.approver.wizard'].with_user(
            self.user_gm).sudo().create({
                'leave_id': leave.id,
                'target_step_id': self.env.ref(
                    'KSW_annual_leave.return_step_pending_hr').id,
                'reason': 'Wrong termination reason — please correct.',
            })
        wizard.action_confirm()

        self.assertEqual(leave.x_annual_approval_state, 'pending_hr')
        self.assertTrue(leave.employee_id.with_context(active_test=False).active)
        self.assertFalse(leave.x_eos_employee_archived)

    def test_reapproval_archives_again(self):
        leave = self._make_leave(offset=32)
        leave.sudo().write({'x_eos_termination_reason': '84'})
        self._advance_to(leave, 'pending_employee_signature')
        leave.sudo().action_draft()
        self.assertTrue(leave.employee_id.with_context(active_test=False).active)

        leave.sudo().write({'x_eos_termination_reason': '84'})
        self._advance_to(leave, 'pending_employee_signature')
        self.assertFalse(leave.employee_id.with_context(active_test=False).active)
        self.assertTrue(leave.x_eos_employee_archived)

    def test_unrelated_archive_is_not_undone(self):
        """An employee archived by HR is not resurrected by a stale EOS draft.

        The flag is what authorises the restore, so a request that never got
        as far as GM final approval leaves them exactly as they were.
        """
        leave = self._make_leave(offset=33)
        leave.sudo().write({'x_eos_termination_reason': '84'})
        self._advance_to(leave, 'pending_acc')
        emp = leave.employee_id
        emp.sudo().with_context(no_wizard=True).action_archive()

        leave.sudo().action_refuse()

        self.assertFalse(leave.x_eos_employee_archived)
        self.assertFalse(emp.with_context(active_test=False).active)
