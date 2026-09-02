"""'On Vacation' opens at GM final approval, and closes with it.

The stamp used to land at Step 6, when HR files the signed vacation form.
That is not when the employee leaves — the GM's signature is, and HR's
filing can trail it by days.  During that gap nothing said the employee was
away: no ribbon, no punch alert, and the attendance sheet's return gate saw
an ordinary month.

So the marker now follows GM final approval in both directions:

  - Opened by   action_gm_final_approve (Step 5), while state is still
                'confirm' — the KSW chain does not reach 'validate' until
                Step 6
  - Survives    HR's confirmation, and the manager's return confirmation
                still closes it
  - Closed by   every route that undoes GM final approval: refuse, reset to
                draft, back to approval, the Cancel wizard, and the GM /
                administrator returning the request to an earlier step
  - Kept by     a return *to* Step 6 — GM final approval was not undone
"""
import base64
from datetime import date, timedelta

from odoo.tests.common import TransactionCase


class TestOnVacationAtGmFinal(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _mkuser(name, login, extra_groups=()):
            group_ids = [cls.env.ref('base.group_user').id]
            group_ids.extend(extra_groups)
            return cls.env['res.users'].create({
                'name': name,
                'login': login,
                'email': f'{login}@onvac.test',
                'group_ids': [(6, 0, group_ids)],
            })

        cls.user_dm = _mkuser('Vac DM', 'vac_dm')
        cls.user_hr = _mkuser(
            'Vac HR', 'vac_hr',
            [cls.env.ref('KSW_annual_leave.group_annual_leave_hr').id])
        cls.user_acc = _mkuser(
            'Vac Acc', 'vac_acc',
            [cls.env.ref('KSW_annual_leave.group_annual_leave_acc').id])
        cls.user_gm = _mkuser(
            'Vac GM', 'vac_gm',
            [cls.env.ref('KSW_annual_leave.group_annual_leave_gm').id])
        cls.user_admin = _mkuser(
            'Vac Admin', 'vac_admin',
            [cls.env.ref('base.group_system').id])

        for user, name in (
            (cls.user_dm, 'Vac DM Emp'), (cls.user_hr, 'Vac HR Emp'),
            (cls.user_acc, 'Vac Acc Emp'), (cls.user_gm, 'Vac GM Emp'),
            (cls.user_admin, 'Vac Admin Emp'),
        ):
            cls.env['hr.employee'].create({'name': name, 'user_id': user.id})

        cls.employee = cls.env['hr.employee'].create({
            'name': 'On Vacation Requesting Employee',
            'user_id': _mkuser('Vac Emp', 'vac_emp').id,
            'leave_manager_id': cls.user_dm.id,
        })

        cls.leave_type = cls.env['hr.leave.type'].create({
            'name': 'Annual Leave On-Vacation Timing Test',
            'requires_allocation': False,
            'leave_validation_type': 'annual_multi',
            'is_annual_leave': True,
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    _STATE_RANK = {
        'pending_dm': 0, 'pending_hr': 1, 'pending_gm_initial': 2,
        'pending_acc': 3, 'pending_gm_final': 4,
        'pending_employee_signature': 5, 'approved': 6,
    }

    def _make_leave(self, offset=0):
        base = date(2029, 1, 1) + timedelta(days=offset * 15)
        return self.env['hr.leave'].sudo().create({
            'employee_id': self.employee.id,
            'holiday_status_id': self.leave_type.id,
            'request_date_from': base,
            'request_date_to': base + timedelta(days=6),
        })

    def _advance_to(self, leave, target_state):
        steps = [
            ('pending_dm',         'action_dm_approve',         self.user_dm),
            ('pending_hr',         'action_hr_approve',         self.user_hr),
            ('pending_gm_initial', 'action_gm_initial_approve', self.user_gm),
            ('pending_acc',        'action_acc_approve',        self.user_acc),
            ('pending_gm_final',   'action_gm_final_approve',   self.user_gm),
        ]
        for pre_state, method, user in steps:
            if (self._STATE_RANK.get(leave.x_annual_approval_state, 0)
                    > self._STATE_RANK[pre_state]):
                continue
            getattr(leave.with_user(user).sudo(), method)()
            if leave.x_annual_approval_state == target_state:
                break
        return leave

    def _hr_confirm(self, leave):
        att = self.env['ir.attachment'].sudo().create({
            'name': 'signed_form_stub.pdf',
            'datas': base64.b64encode(b'stub'),
            'res_model': 'hr.leave',
            'res_id': leave.id,
        })
        leave.sudo().write({'x_attachment_ids': [(4, att.id)]})
        leave.with_user(self.user_hr).sudo().action_employee_confirm_signature()
        return leave

    def _return_to(self, leave, step_xmlid):
        """Drive the return wizard as the Settings Administrator."""
        wizard = self.env['ksw.gm.return.approver.wizard'].with_user(
            self.user_admin).sudo().create({
                'leave_id': leave.id,
                'target_step_id': self.env.ref(step_xmlid).id,
                'reason': 'Figures need revising.',
            })
        wizard.with_user(self.user_admin).action_confirm()
        return leave

    # ==================================================================
    # Opening
    # ==================================================================

    def test_not_on_vacation_before_gm_final(self):
        """Nothing marks the employee as away while the chain is still running."""
        leave = self._advance_to(self._make_leave(), 'pending_gm_final')
        self.assertEqual(leave.x_return_state, 'not_applicable')
        self.assertFalse(leave.x_is_on_vacation)

    def test_on_vacation_at_gm_final(self):
        """GM final approval opens the return — Step 6 has not run yet."""
        leave = self._advance_to(
            self._make_leave(offset=1), 'pending_employee_signature')
        self.assertEqual(leave.state, 'confirm')
        self.assertEqual(
            leave.x_annual_approval_state, 'pending_employee_signature')
        self.assertEqual(leave.x_return_state, 'on_vacation')
        self.assertTrue(
            leave.x_is_on_vacation,
            'x_is_on_vacation must hold at "confirm" — the KSW chain lives '
            'there right through pending_employee_signature.')

    def test_still_on_vacation_after_hr_confirms(self):
        """HR's confirmation validates the leave and leaves the return open."""
        leave = self._hr_confirm(self._advance_to(
            self._make_leave(offset=2), 'pending_employee_signature'))
        self.assertEqual(leave.state, 'validate')
        self.assertEqual(leave.x_return_state, 'on_vacation')

    def test_manager_can_still_confirm_the_return(self):
        """The end of the flow is unchanged: the DM closes the return."""
        leave = self._hr_confirm(self._advance_to(
            self._make_leave(offset=3), 'pending_employee_signature'))
        leave.sudo().write({'x_return_date': date(2029, 2, 8)})
        leave.with_user(self.user_dm).sudo().action_confirm_return_manager()
        self.assertEqual(leave.x_return_state, 'hr_confirmed')
        self.assertFalse(leave.x_is_on_vacation)

    # ==================================================================
    # Closing — every route back out
    # ==================================================================

    def test_refuse_clears_on_vacation(self):
        leave = self._advance_to(
            self._make_leave(offset=4), 'pending_employee_signature')
        leave.sudo().action_refuse()
        self.assertEqual(leave.x_return_state, 'not_applicable')
        self.assertFalse(leave.x_is_on_vacation)

    def test_draft_clears_on_vacation(self):
        leave = self._advance_to(
            self._make_leave(offset=5), 'pending_employee_signature')
        leave.sudo().action_draft()
        self.assertEqual(leave.x_return_state, 'not_applicable')

    def test_back_to_approval_clears_on_vacation(self):
        leave = self._hr_confirm(self._advance_to(
            self._make_leave(offset=6), 'pending_employee_signature'))
        leave.sudo()._move_validate_leave_to_confirm()
        self.assertEqual(leave.x_return_state, 'not_applicable')

    def test_cancel_wizard_clears_on_vacation(self):
        """_force_cancel writes through sudo(), so this needs its own route."""
        leave = self._advance_to(
            self._make_leave(offset=7), 'pending_employee_signature')
        leave.sudo()._action_user_cancel('No longer travelling.')
        self.assertEqual(leave.x_return_state, 'not_applicable')

    def test_return_to_earlier_step_clears_on_vacation(self):
        """The chain is running again, so the employee is not away."""
        leave = self._advance_to(
            self._make_leave(offset=8), 'pending_employee_signature')
        self._return_to(leave, 'KSW_annual_leave.return_step_pending_acc')
        self.assertEqual(leave.x_annual_approval_state, 'pending_acc')
        self.assertEqual(leave.x_return_state, 'not_applicable')

    def test_return_to_hr_confirmation_keeps_on_vacation(self):
        """Only Step 6 is re-opened — GM final approval still stands."""
        leave = self._hr_confirm(self._advance_to(
            self._make_leave(offset=9), 'pending_employee_signature'))
        self._return_to(
            leave, 'KSW_annual_leave.return_step_pending_employee_signature')
        self.assertEqual(
            leave.x_annual_approval_state, 'pending_employee_signature')
        self.assertEqual(leave.x_return_state, 'on_vacation')

    def test_reapproval_reopens_on_vacation(self):
        """Sent back and re-approved: the marker comes back with the signature."""
        leave = self._advance_to(
            self._make_leave(offset=10), 'pending_employee_signature')
        self._return_to(leave, 'KSW_annual_leave.return_step_pending_acc')
        self.assertEqual(leave.x_return_state, 'not_applicable')
        self._advance_to(leave, 'pending_employee_signature')
        self.assertEqual(leave.x_return_state, 'on_vacation')

    def test_confirmed_return_is_never_reopened(self):
        """A manager's confirmation is a fact; re-syncing must not undo it."""
        leave = self._hr_confirm(self._advance_to(
            self._make_leave(offset=11), 'pending_employee_signature'))
        leave.sudo().write({'x_return_date': date(2029, 6, 8)})
        leave.with_user(self.user_dm).sudo().action_confirm_return_manager()
        leave.sudo()._sync_gm_final_state()
        self.assertEqual(leave.x_return_state, 'hr_confirmed')
