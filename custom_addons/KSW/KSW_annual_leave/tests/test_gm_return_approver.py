"""Tests for the GM Return-to-Approver feature.

Covers:
  - Auth:         only GM can open/confirm the wizard; wrong state is rejected
  - Target rules: invalid backward targets raise UserError
  - State change: x_annual_approval_state moves to the requested target
  - Stamp clear:  only stamps at-and-after the target step are cleared
  - Chatter:      a note with the GM's reason is posted on the leave
  - Notification: inbox message with reason is sent to the target approver
  - Gate field:   x_can_gm_return is True only for GM at the two GM steps
  - Integration:  after return the chain can re-run and complete normally
"""
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestGmReturnApprover(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _mkuser(name, login, extra_groups=()):
            group_ids = [cls.env.ref('base.group_user').id]
            group_ids.extend(extra_groups)
            u = cls.env['res.users'].create({
                'name': name,
                'login': login,
                'email': f'{login}@ret.test',
                'group_ids': [(6, 0, group_ids)],
            })
            return u

        cls.user_dm  = _mkuser('Return DM',  'ret_dm')
        cls.user_hr  = _mkuser('Return HR',  'ret_hr',  [cls.env.ref('KSW_annual_leave.group_annual_leave_hr').id])
        cls.user_acc = _mkuser('Return Acc', 'ret_acc', [cls.env.ref('KSW_annual_leave.group_annual_leave_acc').id])
        cls.user_gm  = _mkuser('Return GM',  'ret_gm',  [cls.env.ref('KSW_annual_leave.group_annual_leave_gm').id])

        cls.emp_dm  = cls.env['hr.employee'].create({'name': 'Ret DM Emp',  'user_id': cls.user_dm.id})
        cls.emp_hr  = cls.env['hr.employee'].create({'name': 'Ret HR Emp',  'user_id': cls.user_hr.id})
        cls.emp_acc = cls.env['hr.employee'].create({'name': 'Ret Acc Emp', 'user_id': cls.user_acc.id})
        cls.emp_gm  = cls.env['hr.employee'].create({'name': 'Ret GM Emp',  'user_id': cls.user_gm.id})
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Requesting Employee Return',
            'leave_manager_id': cls.user_dm.id,
        })

        # requires_allocation=False (bool) so _check_validity skips the
        # allocation check. 'no' is a truthy string and would trigger it.
        cls.leave_type = cls.env['hr.leave.type'].create({
            'name': 'Annual Leave GM Return Test',
            'requires_allocation': False,
            'leave_validation_type': 'annual_multi',
            'is_annual_leave': True,
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_leave(self, offset=0):
        """Create a confirmed leave at pending_dm, ready for approval.

        offset (int): shifts the date window by N*15 days so multiple
        leaves can coexist in a single test method without overlapping.
        """
        from datetime import date, timedelta
        base = date(2027, 3, 1) + timedelta(days=offset * 15)
        # In Odoo 19, hr.leave is created directly in 'confirm' state (default='confirm').
        # There is no action_confirm() on the base model.
        # Our create() override sets x_annual_approval_state='pending_dm' automatically.
        return self.env['hr.leave'].sudo().create({
            'employee_id': self.employee.id,
            'holiday_status_id': self.leave_type.id,
            'request_date_from': base,
            'request_date_to': base + timedelta(days=9),
        })

    _STATE_RANK = {
        'pending_dm': 0, 'pending_hr': 1, 'pending_gm_initial': 2,
        'pending_acc': 3, 'pending_gm_final': 4,
        'pending_employee_signature': 5, 'approved': 6,
    }

    def _advance_to(self, leave, target_state):
        """Advance the approval chain to target_state.

        Resume-aware: skips steps the leave has already passed, so it is
        safe to call multiple times on the same leave with increasing targets.
        Uses with_user(correct_user) so _check_group passes for each step.
        """
        steps = [
            ('pending_dm',         'pending_hr',                 'action_dm_approve',         self.user_dm),
            ('pending_hr',         'pending_gm_initial',         'action_hr_approve',         self.user_hr),
            ('pending_gm_initial', 'pending_acc',                'action_gm_initial_approve', self.user_gm),
            ('pending_acc',        'pending_gm_final',           'action_acc_approve',        self.user_acc),
            ('pending_gm_final',   'pending_employee_signature', 'action_gm_final_approve',   self.user_gm),
        ]
        for pre_state, _post_state, method, user in steps:
            if self._STATE_RANK.get(leave.x_annual_approval_state, 0) > self._STATE_RANK.get(pre_state, 0):
                continue  # already past this step — skip
            getattr(leave.with_user(user).sudo(), method)()
            if leave.x_annual_approval_state == target_state:
                break

    def _return_via_wizard(self, leave, target_state, reason='Fix this.'):
        """Open and confirm the return wizard as the GM user."""
        wiz = self.env['ksw.gm.return.approver.wizard'].with_user(
            self.user_gm
        ).create({
            'leave_id': leave.id,
            'target_state': target_state,
            'reason': reason,
        })
        wiz.action_confirm()
        return wiz

    # ==================================================================
    # Auth tests
    # ==================================================================

    def test_non_gm_cannot_open_wizard(self):
        """HR user calling action_open_gm_return_wizard must raise UserError."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_initial')
        with self.assertRaises(UserError):
            leave.with_user(self.user_hr).action_open_gm_return_wizard()

    def test_dm_cannot_open_wizard(self):
        """DM (leave manager) cannot open the return wizard — GM-only."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_initial')
        with self.assertRaises(UserError):
            leave.with_user(self.user_dm).action_open_gm_return_wizard()

    def test_acc_cannot_open_wizard(self):
        """Accounting user cannot open the return wizard."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_final')
        with self.assertRaises(UserError):
            leave.with_user(self.user_acc).action_open_gm_return_wizard()

    def test_non_gm_wizard_confirm_raises(self):
        """A non-GM user calling wizard.action_confirm must raise UserError."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_initial')
        # sudo() to bypass wizard ACL so we can test the method-level guard
        wiz = self.env['ksw.gm.return.approver.wizard'].sudo().create({
            'leave_id': leave.id,
            'target_state': 'pending_hr',
            'reason': 'Fix it.',
        })
        with self.assertRaises(UserError):
            wiz.with_user(self.user_hr).action_confirm()

    def test_dm_wizard_confirm_raises(self):
        """DM calling wizard.action_confirm must raise UserError."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_initial')
        wiz = self.env['ksw.gm.return.approver.wizard'].sudo().create({
            'leave_id': leave.id,
            'target_state': 'pending_dm',
            'reason': 'Fix it.',
        })
        with self.assertRaises(UserError):
            wiz.with_user(self.user_dm).action_confirm()

    def test_acc_wizard_confirm_raises(self):
        """Accounting user calling wizard.action_confirm must raise UserError."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_final')
        wiz = self.env['ksw.gm.return.approver.wizard'].sudo().create({
            'leave_id': leave.id,
            'target_state': 'pending_hr',
            'reason': 'Fix it.',
        })
        with self.assertRaises(UserError):
            wiz.with_user(self.user_acc).action_confirm()

    def test_open_wizard_raises_at_wrong_state(self):
        """action_open_gm_return_wizard raises when leave is not at a GM step."""
        leave = self._make_leave()
        # Still at pending_dm — not a GM step
        with self.assertRaises(UserError):
            leave.with_user(self.user_gm).action_open_gm_return_wizard()

    def test_open_wizard_raises_at_pending_hr(self):
        """Raises at pending_hr too."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_hr')
        with self.assertRaises(UserError):
            leave.with_user(self.user_gm).action_open_gm_return_wizard()

    def test_open_wizard_raises_at_pending_acc(self):
        """Raises at pending_acc — that is an Accounting step, not a GM step."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_acc')
        with self.assertRaises(UserError):
            leave.with_user(self.user_gm).action_open_gm_return_wizard()

    def test_open_wizard_returns_act_window(self):
        """action_open_gm_return_wizard returns a valid ir.actions.act_window."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_initial')
        result = leave.with_user(self.user_gm).action_open_gm_return_wizard()
        self.assertEqual(result.get('type'), 'ir.actions.act_window')
        self.assertEqual(result.get('res_model'), 'ksw.gm.return.approver.wizard')
        self.assertEqual(result['context'].get('default_leave_id'), leave.id)

    # ==================================================================
    # Target validation
    # ==================================================================

    def test_gm_initial_cannot_return_to_acc(self):
        """From pending_gm_initial, returning to pending_acc raises UserError."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_initial')
        with self.assertRaises(UserError):
            self._return_via_wizard(leave, 'pending_acc')

    def test_gm_initial_cannot_return_to_itself(self):
        """From pending_gm_initial, returning to pending_gm_initial raises UserError."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_initial')
        with self.assertRaises(UserError):
            self._return_via_wizard(leave, 'pending_gm_initial')

    def test_gm_initial_can_return_to_dm(self):
        """From pending_gm_initial, returning to pending_dm is valid."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_initial')
        self._return_via_wizard(leave, 'pending_dm')
        self.assertEqual(leave.x_annual_approval_state, 'pending_dm')

    def test_gm_initial_can_return_to_hr(self):
        """From pending_gm_initial, returning to pending_hr is valid."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_initial')
        self._return_via_wizard(leave, 'pending_hr')
        self.assertEqual(leave.x_annual_approval_state, 'pending_hr')

    def test_gm_final_can_return_to_all_four_targets(self):
        """From pending_gm_final, all four backward targets are valid."""
        targets = ('pending_dm', 'pending_hr', 'pending_gm_initial', 'pending_acc')
        for i, target in enumerate(targets):
            with self.subTest(target=target):
                # Each iteration uses non-overlapping dates (offset by 15 days)
                leave = self._make_leave(offset=i)
                self._advance_to(leave, 'pending_gm_final')
                self._return_via_wizard(leave, target)
                self.assertEqual(
                    leave.x_annual_approval_state, target,
                    msg=f'State should be {target} after return from pending_gm_final',
                )

    # ==================================================================
    # State transition
    # ==================================================================

    def test_return_sets_approval_state(self):
        """x_annual_approval_state moves exactly to the requested target."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_initial')
        self._return_via_wizard(leave, 'pending_hr')
        self.assertEqual(leave.x_annual_approval_state, 'pending_hr')

    # ==================================================================
    # Approval-stamp clearing
    # ==================================================================

    def test_return_to_hr_keeps_dm_stamp_clears_rest(self):
        """Returning to pending_hr preserves the DM stamp and clears HR and later stamps.

        Advance to pending_gm_final so all prior stamps (DM, HR, GM Initial, Acc)
        are actually set before testing what the return clears.
        """
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_final')
        self.assertTrue(leave.x_dm_approved_by,           'DM stamp should be set before return')
        self.assertTrue(leave.x_hr_approved_by,           'HR stamp should be set before return')
        self.assertTrue(leave.x_gm_initial_approved_by,   'GM Initial stamp should be set before return')
        self.assertTrue(leave.x_acc_approved_by,          'Acc stamp should be set before return')

        self._return_via_wizard(leave, 'pending_hr')

        self.assertTrue(leave.x_dm_approved_by,              'DM stamp must survive return to HR')
        self.assertFalse(leave.x_hr_approved_by,             'HR stamp must be cleared')
        self.assertFalse(leave.x_gm_initial_approved_by,     'GM Initial stamp must be cleared')
        self.assertFalse(leave.x_acc_approved_by,            'Acc stamp must be cleared')
        self.assertFalse(leave.x_gm_final_approved_by,       'GM Final stamp must be cleared')

    def test_return_to_dm_clears_all_stamps(self):
        """Returning to pending_dm wipes every approval stamp.

        Advance to pending_gm_final so DM, HR, GM Initial, and Acc stamps are
        all set before testing that the return wipes them.
        """
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_final')
        self.assertTrue(leave.x_dm_approved_by)
        self.assertTrue(leave.x_hr_approved_by)
        self.assertTrue(leave.x_gm_initial_approved_by)
        self.assertTrue(leave.x_acc_approved_by)

        self._return_via_wizard(leave, 'pending_dm')

        self.assertFalse(leave.x_dm_approved_by)
        self.assertFalse(leave.x_hr_approved_by)
        self.assertFalse(leave.x_gm_initial_approved_by)
        self.assertFalse(leave.x_acc_approved_by)
        self.assertFalse(leave.x_gm_final_approved_by)

    def test_return_to_acc_keeps_dm_hr_gm_initial_stamps(self):
        """Returning to pending_acc preserves DM, HR, and GM Initial stamps."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_final')
        self.assertTrue(leave.x_acc_approved_by)

        self._return_via_wizard(leave, 'pending_acc')

        self.assertTrue(leave.x_dm_approved_by,          'DM stamp must survive')
        self.assertTrue(leave.x_hr_approved_by,          'HR stamp must survive')
        self.assertTrue(leave.x_gm_initial_approved_by,  'GM Initial stamp must survive')
        self.assertFalse(leave.x_acc_approved_by,        'Acc stamp must be cleared')
        self.assertFalse(leave.x_gm_final_approved_by,   'GM Final stamp must be cleared')

    def test_return_to_gm_initial_from_gm_final_keeps_dm_hr(self):
        """Returning to pending_gm_initial preserves only DM and HR stamps."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_final')

        self._return_via_wizard(leave, 'pending_gm_initial')

        self.assertTrue(leave.x_dm_approved_by,          'DM stamp must survive')
        self.assertTrue(leave.x_hr_approved_by,          'HR stamp must survive')
        self.assertFalse(leave.x_gm_initial_approved_by, 'GM Initial stamp must be cleared')
        self.assertFalse(leave.x_acc_approved_by,        'Acc stamp must be cleared')
        self.assertFalse(leave.x_gm_final_approved_by,   'GM Final stamp must be cleared')

    # ==================================================================
    # Chatter & notification
    # ==================================================================

    def test_return_posts_note_with_reason(self):
        """Return action posts a chatter note containing the GM's reason text."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_initial')
        msg_count_before = len(leave.message_ids)

        self._return_via_wizard(leave, 'pending_hr', reason='Penalty amount is wrong.')

        self.assertGreater(len(leave.message_ids), msg_count_before)
        all_bodies = ' '.join(m.body or '' for m in leave.message_ids)
        self.assertIn('Penalty amount is wrong.', all_bodies)

    def test_return_sends_notification_with_reason_to_target_group(self):
        """Return sends an inbox notification that includes the GM's reason."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_initial')
        self._return_via_wizard(leave, 'pending_hr', reason='Iqama renewal missing.')

        # The notification message has partner_ids set and contains the reason
        notif_msgs = leave.message_ids.filtered(
            lambda m: m.partner_ids and 'Iqama renewal missing.' in (m.body or '')
        )
        self.assertTrue(notif_msgs,
            msg='An inbox notification with the reason must be sent to the HR group')

    def test_return_to_dm_notifies_dm_partner(self):
        """When returning to DM, the DM's partner receives the notification."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_initial')
        self._return_via_wizard(leave, 'pending_dm', reason='Dates need correction.')

        dm_partner = self.user_dm.partner_id
        notif_msgs = leave.message_ids.filtered(
            lambda m: dm_partner in m.partner_ids and 'Dates need correction.' in (m.body or '')
        )
        self.assertTrue(notif_msgs,
            msg='The DM partner should receive the return notification')

    # ==================================================================
    # x_can_gm_return gate
    # ==================================================================

    def test_can_gm_return_true_at_gm_initial(self):
        """x_can_gm_return is True for GM user at pending_gm_initial."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_initial')
        self.assertTrue(leave.with_user(self.user_gm).x_can_gm_return)

    def test_can_gm_return_true_at_gm_final(self):
        """x_can_gm_return is True for GM user at pending_gm_final."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_final')
        self.assertTrue(leave.with_user(self.user_gm).x_can_gm_return)

    def test_can_gm_return_false_at_other_states(self):
        """x_can_gm_return is False for GM user when not at a GM step."""
        leave = self._make_leave()
        # pending_dm
        self.assertFalse(leave.with_user(self.user_gm).x_can_gm_return)
        self._advance_to(leave, 'pending_hr')
        self.assertFalse(leave.with_user(self.user_gm).x_can_gm_return)
        self._advance_to(leave, 'pending_acc')
        self.assertFalse(leave.with_user(self.user_gm).x_can_gm_return)

    def test_can_gm_return_false_at_pending_employee_signature(self):
        """x_can_gm_return is False even for GM after both GM steps are passed."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_employee_signature')
        self.assertFalse(leave.with_user(self.user_gm).x_can_gm_return)

    def test_can_gm_return_false_for_non_gm_users(self):
        """x_can_gm_return is False for non-GM users at any state."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_initial')
        self.assertFalse(leave.with_user(self.user_hr).x_can_gm_return)
        self.assertFalse(leave.with_user(self.user_acc).x_can_gm_return)
        self.assertFalse(leave.with_user(self.user_dm).x_can_gm_return)

    # ==================================================================
    # Integration: chain continues after return
    # ==================================================================

    def test_gm_initial_returns_to_hr_and_chain_completes(self):
        """After GM Initial returns to HR, HR re-approves and the chain completes."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_initial')

        self._return_via_wizard(leave, 'pending_hr', reason='Add flight ticket.')
        self.assertEqual(leave.x_annual_approval_state, 'pending_hr')

        # HR re-approves
        leave.with_user(self.user_hr).sudo().action_hr_approve()
        self.assertEqual(leave.x_annual_approval_state, 'pending_gm_initial')
        self.assertTrue(leave.x_hr_approved_by, 'HR stamp set after re-approval')

        # Continue to completion
        leave.with_user(self.user_gm).sudo().action_gm_initial_approve()
        leave.with_user(self.user_acc).sudo().action_acc_approve()
        leave.with_user(self.user_gm).sudo().action_gm_final_approve()
        self.assertEqual(leave.x_annual_approval_state, 'pending_employee_signature')

    def test_gm_initial_returns_to_dm_and_chain_completes(self):
        """After GM Initial returns to DM, the chain re-runs from the start."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_initial')

        self._return_via_wizard(leave, 'pending_dm', reason='Wrong dates.')
        self.assertEqual(leave.x_annual_approval_state, 'pending_dm')

        # Full chain re-run
        leave.with_user(self.user_dm).sudo().action_dm_approve()
        leave.with_user(self.user_hr).sudo().action_hr_approve()
        leave.with_user(self.user_gm).sudo().action_gm_initial_approve()
        leave.with_user(self.user_acc).sudo().action_acc_approve()
        leave.with_user(self.user_gm).sudo().action_gm_final_approve()
        self.assertEqual(leave.x_annual_approval_state, 'pending_employee_signature')

    def test_gm_final_returns_to_acc_and_chain_completes(self):
        """After GM Final returns to Accounting, Acc re-approves and chain completes."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_final')

        self._return_via_wizard(leave, 'pending_acc', reason='Commission lines missing.')
        self.assertEqual(leave.x_annual_approval_state, 'pending_acc')

        leave.with_user(self.user_acc).sudo().action_acc_approve()
        self.assertEqual(leave.x_annual_approval_state, 'pending_gm_final')

        leave.with_user(self.user_gm).sudo().action_gm_final_approve()
        self.assertEqual(leave.x_annual_approval_state, 'pending_employee_signature')

    def test_gm_final_returns_to_hr_and_chain_completes(self):
        """After GM Final returns to HR, the full chain from HR onwards re-runs."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_final')

        self._return_via_wizard(leave, 'pending_hr', reason='Penalty description missing.')
        self.assertEqual(leave.x_annual_approval_state, 'pending_hr')
        # GM Initial stamp is also cleared
        self.assertFalse(leave.x_gm_initial_approved_by)

        leave.with_user(self.user_hr).sudo().action_hr_approve()
        leave.with_user(self.user_gm).sudo().action_gm_initial_approve()
        leave.with_user(self.user_acc).sudo().action_acc_approve()
        leave.with_user(self.user_gm).sudo().action_gm_final_approve()
        self.assertEqual(leave.x_annual_approval_state, 'pending_employee_signature')

    def test_double_return_then_completion(self):
        """GM can return twice without corrupting the chain."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_gm_final')

        # First return: to HR
        self._return_via_wizard(leave, 'pending_hr', reason='First correction.')
        leave.with_user(self.user_hr).sudo().action_hr_approve()
        leave.with_user(self.user_gm).sudo().action_gm_initial_approve()
        leave.with_user(self.user_acc).sudo().action_acc_approve()
        self.assertEqual(leave.x_annual_approval_state, 'pending_gm_final')

        # Second return: to Acc
        self._return_via_wizard(leave, 'pending_acc', reason='Second correction.')
        leave.with_user(self.user_acc).sudo().action_acc_approve()
        leave.with_user(self.user_gm).sudo().action_gm_final_approve()
        self.assertEqual(leave.x_annual_approval_state, 'pending_employee_signature')
