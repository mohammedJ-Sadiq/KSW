"""Tests for the HR-confirmation step (Step 6) of the annual-leave chain.

After the changes made in July 2026, the final step that was previously
called "Employee Signature" is now handled exclusively by HR.  These tests
verify every behavioural contract that changed:

  - Gate field: x_can_sign is True only for HR users at pending_employee_signature
  - Auth:       only HR can call action_employee_confirm_signature
  - Attachment: an attachment must be present before HR can confirm
  - State:      after confirmation the leave reaches state=approved / validate
  - Stamps:     x_employee_signed_by and x_employee_signed_date are set
  - Notification: after GM final approval the notification goes to HR group,
                  not to the employee or DM
  - Integration: full chain from pending_dm → approved via HR confirmation
"""
import base64
from datetime import date, timedelta

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestHrConfirmationStep(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _mkuser(name, login, extra_groups=()):
            group_ids = [cls.env.ref('base.group_user').id]
            group_ids.extend(extra_groups)
            return cls.env['res.users'].create({
                'name': name,
                'login': login,
                'email': f'{login}@hrconf.test',
                'group_ids': [(6, 0, group_ids)],
            })

        cls.user_dm  = _mkuser('Conf DM',  'conf_dm')
        cls.user_hr  = _mkuser('Conf HR',  'conf_hr',  [cls.env.ref('KSW_annual_leave.group_annual_leave_hr').id])
        cls.user_acc = _mkuser('Conf Acc', 'conf_acc', [cls.env.ref('KSW_annual_leave.group_annual_leave_acc').id])
        cls.user_gm  = _mkuser('Conf GM',  'conf_gm',  [cls.env.ref('KSW_annual_leave.group_annual_leave_gm').id])

        cls.emp_dm  = cls.env['hr.employee'].create({'name': 'Conf DM Emp',  'user_id': cls.user_dm.id})
        cls.emp_hr  = cls.env['hr.employee'].create({'name': 'Conf HR Emp',  'user_id': cls.user_hr.id})
        cls.emp_acc = cls.env['hr.employee'].create({'name': 'Conf Acc Emp', 'user_id': cls.user_acc.id})
        cls.emp_gm  = cls.env['hr.employee'].create({'name': 'Conf GM Emp',  'user_id': cls.user_gm.id})

        # The "requesting" employee is a subordinate of the DM.
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Requesting Employee Conf',
            'user_id': _mkuser('Emp Conf', 'emp_conf').id,
            'leave_manager_id': cls.user_dm.id,
        })
        cls.user_emp = cls.employee.user_id

        cls.leave_type = cls.env['hr.leave.type'].create({
            'name': 'Annual Leave HR Conf Test',
            'requires_allocation': False,
            'leave_validation_type': 'annual_multi',
            'is_annual_leave': True,
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_leave(self, offset=0):
        base = date(2028, 1, 1) + timedelta(days=offset * 15)
        return self.env['hr.leave'].sudo().create({
            'employee_id': self.employee.id,
            'holiday_status_id': self.leave_type.id,
            'request_date_from': base,
            'request_date_to': base + timedelta(days=6),
        })

    _STATE_RANK = {
        'pending_dm': 0, 'pending_hr': 1, 'pending_gm_initial': 2,
        'pending_acc': 3, 'pending_gm_final': 4,
        'pending_employee_signature': 5, 'approved': 6,
    }

    def _advance_to(self, leave, target_state):
        """Advance the approval chain to target_state (resume-aware)."""
        steps = [
            ('pending_dm',         'action_dm_approve',         self.user_dm),
            ('pending_hr',         'action_hr_approve',         self.user_hr),
            ('pending_gm_initial', 'action_gm_initial_approve', self.user_gm),
            ('pending_acc',        'action_acc_approve',        self.user_acc),
            ('pending_gm_final',   'action_gm_final_approve',   self.user_gm),
        ]
        for pre_state, method, user in steps:
            if self._STATE_RANK.get(leave.x_annual_approval_state, 0) > self._STATE_RANK[pre_state]:
                continue
            getattr(leave.with_user(user).sudo(), method)()
            if leave.x_annual_approval_state == target_state:
                break

    def _attach(self, leave):
        """Attach a stub PDF to the leave and link via x_attachment_ids."""
        att = self.env['ir.attachment'].sudo().create({
            'name': 'signed_form_stub.pdf',
            'datas': base64.b64encode(b'stub'),
            'res_model': 'hr.leave',
            'res_id': leave.id,
        })
        leave.sudo().write({'x_attachment_ids': [(4, att.id)]})
        return att

    # ==================================================================
    # x_can_sign gate
    # ==================================================================

    def test_can_sign_true_for_hr_at_pending_signature(self):
        """HR user gets x_can_sign=True when leave is pending_employee_signature."""
        leave = self._make_leave()
        self._advance_to(leave, 'pending_employee_signature')
        self.assertTrue(leave.with_user(self.user_hr).x_can_sign)

    def test_can_sign_false_for_employee_at_pending_signature(self):
        """The requesting employee no longer gets x_can_sign at step 6."""
        leave = self._make_leave(offset=1)
        self._advance_to(leave, 'pending_employee_signature')
        self.assertFalse(leave.with_user(self.user_emp).x_can_sign)

    def test_can_sign_false_for_dm_at_pending_signature(self):
        """The DM no longer gets x_can_sign at step 6."""
        leave = self._make_leave(offset=2)
        self._advance_to(leave, 'pending_employee_signature')
        self.assertFalse(leave.with_user(self.user_dm).x_can_sign)

    def test_can_sign_false_for_acc_at_pending_signature(self):
        """Accounting user does not get x_can_sign at step 6."""
        leave = self._make_leave(offset=3)
        self._advance_to(leave, 'pending_employee_signature')
        self.assertFalse(leave.with_user(self.user_acc).x_can_sign)

    def test_can_sign_false_for_gm_at_pending_signature(self):
        """GM (without HR group) does not get x_can_sign at step 6."""
        leave = self._make_leave(offset=4)
        self._advance_to(leave, 'pending_employee_signature')
        self.assertFalse(leave.with_user(self.user_gm).x_can_sign)

    def test_can_sign_false_for_hr_at_other_states(self):
        """HR user gets x_can_sign=False when leave is NOT at step 6."""
        leave = self._make_leave(offset=5)
        # pending_dm
        self.assertFalse(leave.with_user(self.user_hr).x_can_sign)
        self._advance_to(leave, 'pending_hr')
        self.assertFalse(leave.with_user(self.user_hr).x_can_sign)
        self._advance_to(leave, 'pending_gm_final')
        self.assertFalse(leave.with_user(self.user_hr).x_can_sign)

    # ==================================================================
    # Auth guard on action_employee_confirm_signature
    # ==================================================================

    def test_employee_cannot_confirm(self):
        """The requesting employee is no longer allowed to call step 6."""
        leave = self._make_leave(offset=10)
        self._advance_to(leave, 'pending_employee_signature')
        self._attach(leave)
        with self.assertRaises(UserError):
            leave.with_user(self.user_emp).action_employee_confirm_signature()

    def test_dm_cannot_confirm(self):
        """The DM is no longer allowed to call step 6."""
        leave = self._make_leave(offset=11)
        self._advance_to(leave, 'pending_employee_signature')
        self._attach(leave)
        with self.assertRaises(UserError):
            leave.with_user(self.user_dm).action_employee_confirm_signature()

    def test_acc_cannot_confirm(self):
        """Accounting user cannot call step 6."""
        leave = self._make_leave(offset=12)
        self._advance_to(leave, 'pending_employee_signature')
        self._attach(leave)
        with self.assertRaises(UserError):
            leave.with_user(self.user_acc).action_employee_confirm_signature()

    def test_gm_cannot_confirm(self):
        """GM (without HR group) cannot call step 6."""
        leave = self._make_leave(offset=13)
        self._advance_to(leave, 'pending_employee_signature')
        self._attach(leave)
        with self.assertRaises(UserError):
            leave.with_user(self.user_gm).action_employee_confirm_signature()

    def test_wrong_state_raises_for_hr(self):
        """Calling action_employee_confirm_signature at the wrong state raises."""
        leave = self._make_leave(offset=14)
        self._advance_to(leave, 'pending_gm_final')
        self._attach(leave)
        with self.assertRaises(UserError):
            leave.with_user(self.user_hr).action_employee_confirm_signature()

    def test_hr_without_attachment_raises(self):
        """HR calling step 6 without an attachment must raise UserError."""
        leave = self._make_leave(offset=15)
        self._advance_to(leave, 'pending_employee_signature')
        with self.assertRaises(UserError):
            leave.with_user(self.user_hr).action_employee_confirm_signature()

    # ==================================================================
    # Success path
    # ==================================================================

    def test_hr_with_attachment_succeeds(self):
        """HR + attachment → x_annual_approval_state becomes 'approved'."""
        leave = self._make_leave(offset=20)
        self._advance_to(leave, 'pending_employee_signature')
        self._attach(leave)
        leave.with_user(self.user_hr).sudo().action_employee_confirm_signature()
        self.assertEqual(leave.x_annual_approval_state, 'approved')

    def test_confirmation_sets_signed_by_stamp(self):
        """x_employee_signed_by is set to the HR user's employee record."""
        leave = self._make_leave(offset=21)
        self._advance_to(leave, 'pending_employee_signature')
        self._attach(leave)
        leave.with_user(self.user_hr).sudo().action_employee_confirm_signature()
        self.assertEqual(leave.x_employee_signed_by, self.emp_hr)

    def test_confirmation_sets_signed_date_stamp(self):
        """x_employee_signed_date is set after HR confirms."""
        leave = self._make_leave(offset=22)
        self._advance_to(leave, 'pending_employee_signature')
        self._attach(leave)
        leave.with_user(self.user_hr).sudo().action_employee_confirm_signature()
        self.assertTrue(leave.x_employee_signed_date)

    def test_confirmation_validates_odoo_state(self):
        """After HR confirmation the underlying hr.leave.state becomes 'validate'."""
        leave = self._make_leave(offset=23)
        self._advance_to(leave, 'pending_employee_signature')
        self._attach(leave)
        leave.with_user(self.user_hr).sudo().action_employee_confirm_signature()
        self.assertEqual(leave.state, 'validate')

    def test_confirmation_posts_chatter_note(self):
        """A chatter note is posted after HR confirmation."""
        leave = self._make_leave(offset=24)
        self._advance_to(leave, 'pending_employee_signature')
        self._attach(leave)
        msg_count_before = len(leave.message_ids)
        leave.with_user(self.user_hr).sudo().action_employee_confirm_signature()
        self.assertGreater(len(leave.message_ids), msg_count_before)

    # ==================================================================
    # Notification routing after GM final approval
    # ==================================================================

    def test_gm_final_notifies_hr_group(self):
        """After GM final approval the notification reaches an HR group member."""
        leave = self._make_leave(offset=30)
        self._advance_to(leave, 'pending_gm_final')
        hr_partner = self.user_hr.partner_id

        leave.with_user(self.user_gm).sudo().action_gm_final_approve()

        notif_msgs = leave.message_ids.filtered(
            lambda m: hr_partner in m.partner_ids
        )
        self.assertTrue(
            notif_msgs,
            msg='HR partner must be notified after GM final approval',
        )

    def test_gm_final_does_not_notify_employee(self):
        """Step 6 notification from GM final approval does not target the employee."""
        leave = self._make_leave(offset=31)
        self._advance_to(leave, 'pending_gm_final')
        emp_partner = self.user_emp.partner_id
        # Snapshot existing message IDs so we only check what action_gm_final_approve adds.
        existing_ids = leave.message_ids.ids

        leave.with_user(self.user_gm).sudo().action_gm_final_approve()

        new_msgs = leave.message_ids.filtered(lambda m: m.id not in existing_ids)
        notif_msgs = new_msgs.filtered(lambda m: emp_partner in m.partner_ids)
        self.assertFalse(
            notif_msgs,
            msg='Employee partner must NOT be in step 6 notification partner_ids',
        )

    def test_gm_final_does_not_notify_dm(self):
        """Step 6 notification from GM final approval does not target the DM."""
        leave = self._make_leave(offset=32)
        self._advance_to(leave, 'pending_gm_final')
        dm_partner = self.user_dm.partner_id
        # The DM was notified at step 1 (creation); snapshot before step 5 action.
        existing_ids = leave.message_ids.ids

        leave.with_user(self.user_gm).sudo().action_gm_final_approve()

        new_msgs = leave.message_ids.filtered(lambda m: m.id not in existing_ids)
        notif_msgs = new_msgs.filtered(lambda m: dm_partner in m.partner_ids)
        self.assertFalse(
            notif_msgs,
            msg='DM partner must NOT be in step 6 notification partner_ids',
        )

    # ==================================================================
    # Integration: full chain from pending_dm to approved
    # ==================================================================

    def test_full_chain_completes_via_hr_confirmation(self):
        """Full 6-step chain runs end-to-end with HR performing the last step."""
        leave = self._make_leave(offset=40)

        # Steps 1-5 via _advance_to
        self._advance_to(leave, 'pending_employee_signature')
        self.assertEqual(leave.x_annual_approval_state, 'pending_employee_signature')

        # Step 6: HR uploads attachment and confirms
        self._attach(leave)
        leave.with_user(self.user_hr).sudo().action_employee_confirm_signature()

        self.assertEqual(leave.x_annual_approval_state, 'approved')
        self.assertEqual(leave.state, 'validate')
        self.assertTrue(leave.x_dm_approved_by)
        self.assertTrue(leave.x_hr_approved_by)
        self.assertTrue(leave.x_gm_initial_approved_by)
        self.assertTrue(leave.x_acc_approved_by)
        self.assertTrue(leave.x_gm_final_approved_by)
        self.assertTrue(leave.x_employee_signed_by)
        self.assertTrue(leave.x_employee_signed_date)
