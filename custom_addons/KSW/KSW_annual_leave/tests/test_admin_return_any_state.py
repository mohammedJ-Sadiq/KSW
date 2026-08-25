"""Tests: the Settings Administrator can return any request to any step.

Counterpart to [TestFinalisedLeaveLock]. Once a request is finalised nobody
but `base.group_system` may reverse it — so the administrator needs a way to
put a request back where it belongs, including at **GM Final**, which the GM's
own return wizard never allowed.

Two invariants the tests pin down:

  * the figures the approvers already entered (penalty, iqama, flight ticket,
    remaining loans) **survive** the return — otherwise returning to GM Final
    would hand the GM an empty request to approve;
  * a request whose payslip is already **confirmed (paid)** is refused, because
    cancelling a paid slip re-collects its installments the next run
    (KSWCO SLIP/11307).
"""
from datetime import date, timedelta

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestAdminReturnAnyState(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _mkuser(name, login, groups=()):
            return cls.env['res.users'].create({
                'name': name,
                'login': login,
                'email': f'{login}@adminret.test',
                'group_ids': [(6, 0, [cls.env.ref('base.group_user').id]
                               + [cls.env.ref(g).id for g in groups])],
            })

        cls.user_dm = _mkuser('Ret DM', 'adminret_dm')
        cls.env['hr.employee'].create({
            'name': 'Ret DM Emp', 'user_id': cls.user_dm.id})

        cls.user_gm = _mkuser('Ret GM', 'adminret_gm',
                              ('KSW_annual_leave.group_annual_leave_gm',))
        cls.user_hr = _mkuser('Ret HR', 'adminret_hr',
                              ('KSW_annual_leave.group_annual_leave_hr',))
        # Nothing but Settings Administrator: no Time Off Administrator, no
        # KSW tier picked by hand. The leave scope has to arrive through the
        # base.group_system -> group_leave_officer implication declared in
        # KSW_annual_leave/security/security.xml, which is exactly what makes
        # the Python guards that name this group actually exercisable.
        cls.user_admin = _mkuser('Ret Sys Admin', 'adminret_admin',
                                 ('base.group_system',))

        # The GM steps follow the employee's department, not the GM group.
        # The fixture GM must be named on the requester's department or he
        # authorises nothing — see tests/test_department_gm.py.
        cls.emp_gm = cls.env['hr.employee'].create({
            'name': 'Ret GM Emp', 'user_id': cls.user_gm.id})
        cls.department = cls.env['hr.department'].create({
            'name': 'Admin Return Test Dept', 'x_gm_id': cls.emp_gm.id})
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Ret Requesting Employee',
            'user_id': _mkuser('Ret Emp', 'adminret_emp').id,
            'leave_manager_id': cls.user_dm.id,
            'department_id': cls.department.id,
        })
        cls.user_emp = cls.employee.user_id

        cls.annual_type = cls.env['hr.leave.type'].create({
            'name': 'Annual Leave Admin-Return Test',
            'requires_allocation': False,
            'leave_validation_type': 'annual_multi',
            'is_annual_leave': True,
        })
        cls.plain_type = cls.env['hr.leave.type'].create({
            'name': 'Plain Leave Admin-Return Test',
            'requires_allocation': False,
            'leave_validation_type': 'hr',
        })

    def setUp(self):
        super().setUp()
        self._slot = 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _leave(self, leave_type=None, days=3):
        self._slot += 1
        start = date.today() + timedelta(days=10 * self._slot)
        return self.env['hr.leave'].sudo().create({
            'employee_id': self.employee.id,
            'holiday_status_id': (leave_type or self.annual_type).id,
            'request_date_from': start,
            'request_date_to': start + timedelta(days=days),
        })

    # The figures the chain collects on the way to GM Final.
    _FIGURES = {
        'x_penalty_amount': 150.0,
        'x_penalty_description': 'Late returns',
        'x_iqama_renewal_amount': 650.0,
        'x_remaining_loans': 1200.0,
        'x_flight_ticket_amount': 900.0,
    }

    def _fully_approved(self, validated=False):
        """A request that cleared GM final approval, with figures + stamps."""
        leave = self._leave()
        dm_emp = self.user_dm.employee_id
        leave.sudo().write({
            'x_annual_approval_state': 'pending_employee_signature',
            'x_dm_approved_by': dm_emp.id,
            'x_hr_approved_by': dm_emp.id,
            'x_gm_initial_approved_by': dm_emp.id,
            'x_acc_approved_by': dm_emp.id,
            'x_gm_final_approved_by': dm_emp.id,
            **self._FIGURES,
        })
        if validated:
            leave.sudo().write({
                'state': 'validate',
                'x_annual_approval_state': 'approved',
                'x_employee_signed_by': dm_emp.id,
            })
        return leave

    def _wizard(self, leave, user=None):
        """The wizard as the form opens it — with default_leave_id in context,
        which is what narrows the radio options."""
        return self.env['ksw.gm.return.approver.wizard'].with_user(
            user or self.user_admin).with_context(default_leave_id=leave.id)

    def _step(self, code):
        return self.env.ref('KSW_annual_leave.return_step_%s' % code)

    def _offered_targets(self, leave, user=None):
        """The steps the radio actually lists for this record.

        This is the domain the widget resolves — `allowed_step_ids` on the
        wizard record itself, not a `fields_get` payload (which the web client
        fetches context-free and caches per model).
        """
        wizard = self._wizard(leave, user).create({
            'leave_id': leave.id,
            'target_step_id': self._step('pending_dm').id,
            'reason': 'probe',
        })
        return wizard.allowed_step_ids.mapped('code')

    def _return(self, leave, target, user=None, reason='Please re-check'):
        wizard = self._wizard(leave, user).create({
            'leave_id': leave.id,
            'target_step_id': self._step(target).id,
            'reason': reason,
        })
        return wizard.action_confirm()

    def _return_over_rpc(self, leave, target, user=None):
        """Build the wizard *without* the record context — the way a
        hand-rolled RPC call would arrive. Exercises the server-side guard
        rather than the UI filtering."""
        wizard = self.env['ksw.gm.return.approver.wizard'].with_user(
            user or self.user_admin).create({
                'leave_id': leave.id,
                'target_step_id': self._step(target).id,
                'reason': 'Please re-check',
            })
        return wizard.action_confirm()

    # ==================================================================
    # Only the steps already passed are offered
    # ==================================================================

    def test_offers_only_steps_already_passed(self):
        """At GM Final, the request has not reached GM Final's *decision* —
        so neither that step nor HR Confirmation is a place to return to."""
        leave = self._leave()
        leave.sudo().write({'x_annual_approval_state': 'pending_gm_final'})

        self.assertEqual(
            self._offered_targets(leave),
            ['pending_dm', 'pending_hr', 'pending_gm_initial', 'pending_acc'])

    def test_current_step_is_never_offered(self):
        for step in ('pending_hr', 'pending_gm_initial', 'pending_acc',
                     'pending_gm_final'):
            leave = self._leave()
            leave.sudo().write({'x_annual_approval_state': step})
            self.assertNotIn(step, self._offered_targets(leave),
                             '%s must not offer itself' % step)

    def test_later_steps_are_never_offered(self):
        leave = self._leave()
        leave.sudo().write({'x_annual_approval_state': 'pending_hr'})
        offered = self._offered_targets(leave)
        for later in ('pending_gm_initial', 'pending_acc', 'pending_gm_final',
                      'pending_employee_signature'):
            self.assertNotIn(later, offered)

    def test_finalised_request_offers_the_whole_chain(self):
        """Past the end of the chain, every step is behind you — including
        GM Final, which is the whole point of the feature."""
        leave = self._fully_approved(validated=True)
        self.assertEqual(self._offered_targets(leave), list(
            ('pending_dm', 'pending_hr', 'pending_gm_initial', 'pending_acc',
             'pending_gm_final', 'pending_employee_signature')))

    def test_plain_target_is_not_offered_on_a_chain_request(self):
        """'Back to Approval' is meaningless where a chain says who acts."""
        leave = self._fully_approved(validated=True)
        self.assertNotIn('confirm', self._offered_targets(leave))

    def test_first_step_has_nowhere_to_return_to(self):
        leave = self._leave()
        leave.sudo().write({'x_annual_approval_state': 'pending_dm'})
        self.assertEqual(self._offered_targets(leave), [])
        self.assertFalse(leave.with_user(self.user_admin).x_can_admin_return,
                         'the button must not be offered with no target')

    def test_gm_sees_the_same_list_as_before(self):
        leave = self._leave()
        leave.sudo().write({'x_annual_approval_state': 'pending_gm_final'})
        self.assertEqual(
            self._offered_targets(leave, user=self.user_gm),
            ['pending_dm', 'pending_hr', 'pending_gm_initial', 'pending_acc'])

        leave2 = self._leave()
        leave2.sudo().write({'x_annual_approval_state': 'pending_gm_initial'})
        self.assertEqual(
            self._offered_targets(leave2, user=self.user_gm),
            ['pending_dm', 'pending_hr'])

    # ==================================================================
    # The headline case: a validated request back to GM Final
    # ==================================================================

    def test_admin_returns_validated_request_to_gm_final(self):
        leave = self._fully_approved(validated=True)
        self._return(leave, 'pending_gm_final')

        self.assertEqual(leave.state, 'confirm',
                         'the Odoo validation must be undone')
        self.assertEqual(leave.x_annual_approval_state, 'pending_gm_final')

    def test_entered_figures_survive_the_return(self):
        """The whole point: the GM must see the numbers again, not a blank."""
        leave = self._fully_approved(validated=True)
        self._return(leave, 'pending_gm_final')

        for field, value in self._FIGURES.items():
            self.assertEqual(
                leave[field], value,
                '%s must survive an admin return' % field)

    def test_return_clears_the_target_stamp_and_later_ones_only(self):
        leave = self._fully_approved(validated=True)
        self._return(leave, 'pending_gm_final')

        # Cleared: the target step's own stamp and everything after it.
        self.assertFalse(leave.x_gm_final_approved_by)
        self.assertFalse(leave.x_employee_signed_by)
        # Kept: every earlier approval.
        self.assertTrue(leave.x_dm_approved_by)
        self.assertTrue(leave.x_hr_approved_by)
        self.assertTrue(leave.x_gm_initial_approved_by)
        self.assertTrue(leave.x_acc_approved_by)

    def test_admin_returns_to_an_early_step(self):
        leave = self._fully_approved()
        self._return(leave, 'pending_hr')

        self.assertEqual(leave.x_annual_approval_state, 'pending_hr')
        self.assertTrue(leave.x_dm_approved_by, 'the DM step is untouched')
        self.assertFalse(leave.x_hr_approved_by)
        self.assertFalse(leave.x_gm_final_approved_by)

    def test_admin_returns_to_the_hr_confirmation_step(self):
        leave = self._fully_approved(validated=True)
        self._return(leave, 'pending_employee_signature')

        self.assertEqual(leave.state, 'confirm')
        self.assertEqual(
            leave.x_annual_approval_state, 'pending_employee_signature')
        self.assertTrue(leave.x_gm_final_approved_by,
                        'GM final approval stands when returning after it')
        self.assertFalse(leave.x_employee_signed_by)

    def test_return_notifies_the_target_approver(self):
        leave = self._fully_approved()
        existing_ids = leave.message_ids.ids
        self._return(leave, 'pending_gm_final', reason='Recheck the ticket')

        new_msgs = leave.message_ids.filtered(
            lambda m: m.id not in existing_ids)
        gm_partner = self.user_gm.partner_id
        self.assertTrue(
            new_msgs.filtered(lambda m: gm_partner in m.partner_ids),
            'the GM should get an inbox notification')
        self.assertTrue(
            any('Recheck the ticket' in (m.body or '') for m in new_msgs))

    # ==================================================================
    # Plain (non-chain) leave types
    # ==================================================================

    def test_admin_returns_plain_leave_to_approval_queue(self):
        leave = self._leave(self.plain_type)
        leave.sudo().write({'state': 'validate'})
        self._return(leave, 'confirm')
        self.assertEqual(leave.state, 'confirm')

    def test_plain_leave_offers_only_back_to_approval(self):
        """There is no chain to return a sick leave to."""
        leave = self._leave(self.plain_type)
        leave.sudo().write({'state': 'validate'})
        self.assertEqual(self._offered_targets(leave), ['confirm'])
        with self.assertRaises(UserError):
            self._return_over_rpc(leave, 'pending_gm_final')
        self.assertEqual(leave.state, 'validate')

    # ==================================================================
    # Guards
    # ==================================================================

    # The "confirmed payslip blocks the return" case lives in
    # KSW_payroll/tests/test_leave_payslip_lock.py — hr.payslip only exists
    # once KSW_payroll is installed.

    def test_hr_approver_cannot_use_the_wizard(self):
        leave = self._fully_approved()
        with self.assertRaises(UserError):
            self._return_over_rpc(leave, 'pending_hr', user=self.user_hr)
        self.assertEqual(
            leave.x_annual_approval_state, 'pending_employee_signature')

    def test_employee_cannot_use_the_wizard(self):
        leave = self._fully_approved()
        with self.assertRaises(Exception):
            self._return(leave, 'pending_dm', user=self.user_emp)

    # ==================================================================
    # No regression for the GM's own return
    # ==================================================================

    def test_gm_return_still_works(self):
        leave = self._leave()
        leave.sudo().write({'x_annual_approval_state': 'pending_gm_final'})
        self._return(leave, 'pending_dm', user=self.user_gm)
        self.assertEqual(leave.x_annual_approval_state, 'pending_dm')

    def test_gm_cannot_reach_the_admin_only_targets(self):
        """The GM's reachable list is unchanged — no self-return, no signature."""
        for target in ('pending_gm_final', 'pending_employee_signature'):
            leave = self._leave()
            leave.sudo().write({'x_annual_approval_state': 'pending_gm_final'})
            with self.assertRaises(UserError):
                self._return_over_rpc(leave, target, user=self.user_gm)
            self.assertEqual(
                leave.x_annual_approval_state, 'pending_gm_final',
                'GM must not be able to return to %s' % target)

    def test_gm_cannot_use_the_wizard_outside_a_gm_step(self):
        leave = self._fully_approved()
        self.assertEqual(self._offered_targets(leave, user=self.user_gm), [])
        with self.assertRaises(UserError):
            self._return_over_rpc(leave, 'pending_hr', user=self.user_gm)

    # ==================================================================
    # The two admin gaps closed alongside
    # ==================================================================

    def test_settings_admin_gets_leave_scope_by_implication(self):
        """Holding base.group_system alone must be enough to reach a leave.

        KSW neutralises Odoo's own Time Off rules, so without the declared
        implication the administrator would hold every reversal right in
        Python and no record-rule scope to use it on.
        """
        self.assertTrue(self.user_admin.has_group(
            'KSW_annual_leave.group_leave_officer'))
        self.assertFalse(self.user_admin.has_group(
            'hr_holidays.group_hr_holidays_manager'))

    def test_admin_can_delete_validated_leave(self):
        leave = self._leave()
        leave.sudo().write({'state': 'validate'})
        leave.with_user(self.user_admin).unlink()
        self.assertFalse(leave.exists())

    def test_admin_can_cancel_another_employees_validated_leave(self):
        leave = self._leave(self.plain_type)
        leave.sudo().write({'state': 'validate'})
        self.assertTrue(
            leave.with_user(self.user_admin).can_cancel,
            'core only offers Cancel on own leaves; the admin needs it on any')
        leave.with_user(self.user_admin)._action_user_cancel('admin cancel')
        self.assertEqual(leave.state, 'cancel')
