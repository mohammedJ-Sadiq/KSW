"""Partial approval — sub-batches and the GM's partial return.

The month used to be approvable only per department, and handing a
department over froze every batch in it. Two things the business needs did
not fit:

* a supervisor handing over **one employee** mid-month (he leaves on
  vacation, and the settlement only pays what the GM has approved) while he
  keeps typing for everybody else;
* a GM refusing **one employee's overtime** and approving the rest, on a
  normal end-of-month handover.

Both need the decision on the row. These tests pin that down: what a
sub-batch takes, what it locks and what it leaves open, what the GM sees,
that nothing approved can be deleted, and that nothing paid can be reopened.
"""
from odoo.exceptions import UserError, ValidationError

from .test_submission import SubmissionCommon


class SubBatchCommon(SubmissionCommon):

    def setUp(self):
        super().setUp()
        # What a supervisor carries in production: without it his reads of
        # hr.employee go through the public model and never reach a record
        # rule (see test_pay_employee_access.py).
        self.sup_a.sudo().write({'group_ids': [(4, self.env.ref(
            'KSW_base_security.group_hr_employee_subordinate').id)]})
        self.emp_a2 = self._employee('Sub Emp A2', self.dept_a, 6000.0)
        self.ot = self._batch(self.sup_a, self.dept_a)
        self.meals_batch = self._batch(
            self.sup_a, self.dept_a, component=self.meals)
        self.ot_a = self._entry(self.ot, self.emp_a, user=self.sup_a)
        self.ot_a2 = self._entry(self.ot, self.emp_a2, user=self.sup_a)
        self.meal_a = self._entry(
            self.meals_batch, self.emp_a, user=self.sup_a)
        self.submission = self.ot.submission_id
        self.run = self.submission.run_id

    def _sub_batch(self, employees, user=None):
        return self.env['ksw.pay.sub.batch'].with_user(
            user or self.sup_a).create({
                'submission_id': self.submission.id,
                'employee_ids': [(6, 0, employees.ids)],
                'note': 'Annual leave from the 12th',
            })

    def _wizard(self, source=None):
        """Open the supervisor's submit dialog the way the buttons do."""
        source = source or self.submission
        method = ('action_submit_my_departments'
                  if source._name == 'ksw.pay.run' else 'action_submit_prompt')
        action = getattr(source.with_user(self.sup_a), method)()
        self.assertEqual(action['res_model'], 'ksw.pay.submit.wizard')
        return self.env['ksw.pay.submit.wizard'].with_user(
            self.sup_a).browse(action['res_id'])

    def _lines(self, container, mode='review'):
        """Open the GM's review screen the way the button does."""
        action = self.env['ksw.pay.gm.review.wizard'].with_user(
            self.gm)._open_for(container, mode)
        self.assertEqual(action['res_model'], 'ksw.pay.gm.review.line')
        return self.env['ksw.pay.gm.review.line'].with_user(self.gm).search(
            action['domain'])

    def _send_back(self, lines, reason='Wrong hours'):
        """Return… (or Reopen…) on the ticked lines, then the reason."""
        action = (lines.action_reopen() if lines.wizard_id.mode == 'reopen'
                  else lines.action_return())
        dialog = self.env['ksw.pay.gm.review.reason'].with_user(
            self.gm).browse(action['res_id'])
        dialog.reason = reason
        return dialog.action_confirm()

    def _review(self, container, mode, pick, reason='Wrong hours',
                approve_rest=False):
        """Tick ``pick``'s lines and send them back; optionally approve the
        rest in the same screen."""
        lines = self._lines(container, 'reopen' if mode == 'reopen'
                            else 'review')
        chosen = lines.filtered(
            lambda l: l.employee_name in pick.mapped('name'))
        self._send_back(chosen, reason)
        if approve_rest and (lines - chosen).exists():
            (lines - chosen).exists().action_approve()


class TestSubBatchScope(SubBatchCommon):

    def test_01_takes_the_employees_rows_across_every_batch(self):
        sub = self._sub_batch(self.emp_a)
        self.assertEqual(sub.entry_ids, self.ot_a | self.meal_a)

    def test_02_a_row_typed_while_it_is_open_joins_it(self):
        sub = self._sub_batch(self.emp_a)
        late = self._entry(self.ot, self.emp_a, user=self.sup_a)
        self.assertEqual(late.x_sub_batch_id, sub)

    def test_03_an_employee_sits_in_one_open_sub_batch_at_a_time(self):
        self._sub_batch(self.emp_a)
        with self.assertRaises(ValidationError):
            self._sub_batch(self.emp_a)

    def test_04_another_departments_supervisor_cannot_make_one(self):
        with self.assertRaises(UserError):
            self._sub_batch(self.emp_a, user=self.sup_b)


class TestSubBatchLock(SubBatchCommon):

    def test_10_submitting_locks_only_its_rows(self):
        sub = self._sub_batch(self.emp_a)
        sub.with_user(self.sup_a).action_submit()
        self.assertEqual(sub.state, 'submitted')
        self.assertEqual((self.ot_a | self.meal_a).mapped('state'),
                         ['submitted', 'submitted'])
        # The batches stay open…
        self.assertEqual(self.ot.state, 'draft')
        self.assertEqual(self.meals_batch.state, 'draft')
        # …so everybody else is still editable, and new rows can be added…
        self.ot_a2.with_user(self.sup_a).write({'quantity': 7.0})
        self._entry(self.ot, self.emp_a2, user=self.sup_a)
        # …but the handed-over rows are not.
        with self.assertRaises(UserError):
            self.ot_a.with_user(self.sup_a).write({'quantity': 9.0})
        with self.assertRaises(UserError):
            self.ot_a.with_user(self.sup_a).unlink()

    def test_11_the_gm_sees_only_what_was_handed_over(self):
        sub = self._sub_batch(self.emp_a)
        sub.with_user(self.sup_a).action_submit()
        self.submission.invalidate_recordset()
        self.assertEqual(self.submission.pending_entry_ids,
                         self.ot_a | self.meal_a)
        self.run.invalidate_recordset()
        self.assertIn(self.submission,
                      self.run.with_user(self.gm).x_gm_submission_ids)

    def test_12_approval_locks_them_and_leaves_the_month_open(self):
        sub = self._sub_batch(self.emp_a)
        sub.with_user(self.sup_a).action_submit()
        sub.with_user(self.gm).action_approve()
        self.assertEqual(sub.state, 'approved')
        self.assertEqual((self.ot_a | self.meal_a).mapped('state'),
                         ['approved', 'approved'])
        self.assertEqual(self.submission.state, 'draft')
        self.assertNotIn(self.run.state, ('approved', 'paid'))
        # A new row for the same man is new work, typed as usual.
        more = self._entry(self.ot, self.emp_a, user=self.sup_a)
        self.assertEqual(more.state, 'draft')
        self.assertFalse(more.x_sub_batch_id)

    def test_13_a_batch_with_approved_rows_cannot_be_deleted_by_anyone(self):
        sub = self._sub_batch(self.emp_a)
        sub.with_user(self.sup_a).action_submit()
        sub.with_user(self.gm).action_approve()
        with self.assertRaises(UserError):
            self.ot.with_user(self.sup_a).unlink()
        with self.assertRaises(UserError):
            self.ot.sudo().unlink()
        with self.assertRaises(UserError):
            self.ot_a.sudo().unlink()
        with self.assertRaises(UserError):
            sub.sudo().unlink()

    def test_14_only_the_departments_gm_decides_it(self):
        other = self._user('sub_other_gm', 'KSW_commissions.group_commission_gm')
        sub = self._sub_batch(self.emp_a)
        sub.with_user(self.sup_a).action_submit()
        with self.assertRaises(UserError):
            sub.with_user(other).action_approve()
        with self.assertRaises(UserError):
            sub.with_user(self.sup_a).action_approve()

    def test_15_reopening_the_batch_leaves_sub_batch_rows_alone(self):
        sub = self._sub_batch(self.emp_a)
        sub.with_user(self.sup_a).action_submit()
        self.ot.with_user(self.sup_a).action_submit()
        self.assertEqual(self.ot_a2.state, 'submitted')
        self.ot.with_user(self.sup_a).action_reset_to_draft()
        self.assertEqual(self.ot_a2.state, 'draft')
        self.assertEqual(self.ot_a.state, 'submitted')


class TestSubmitPrompt(SubBatchCommon):

    def test_20_submit_always_asks(self):
        wizard = self._wizard()
        self.assertFalse(wizard.has_open_sub_batch)
        self.assertEqual(self.submission.state, 'draft')
        wizard.action_confirm()
        self.assertEqual(self.submission.state, 'submitted')

    def test_21_an_open_sub_batch_is_offered(self):
        self._sub_batch(self.emp_a)
        self.assertTrue(self._wizard().has_open_sub_batch)

    def test_22_only_the_sub_batch(self):
        sub = self._sub_batch(self.emp_a)
        wizard = self._wizard()
        wizard.write({'mode': 'some', 'sub_batch_ids': [(6, 0, sub.ids)]})
        wizard.action_confirm()
        self.assertEqual(sub.state, 'submitted')
        self.assertEqual(self.submission.state, 'draft')
        self.assertEqual(self.ot_a2.state, 'draft')

    def test_23_the_whole_department(self):
        sub = self._sub_batch(self.emp_a)
        wizard = self._wizard()
        wizard.mode = 'all'
        wizard.action_confirm()
        self.assertEqual(self.submission.state, 'submitted')
        self.assertEqual(sub.state, 'submitted')
        self.assertEqual(
            set((self.ot_a | self.ot_a2 | self.meal_a).mapped('state')),
            {'submitted'})


class TestPickComponents(SubBatchCommon):
    """From the Monthly Pay Run, the supervisor sends only some components;
    the others stay open for him."""

    def _send(self, components):
        wizard = self._wizard(self.run)
        wizard.write({'mode': 'some',
                      'component_ids': [(6, 0, components.ids)]})
        wizard.action_confirm()

    def test_50_only_the_ticked_component_goes(self):
        wizard = self._wizard(self.run)
        self.assertEqual(wizard.allowed_component_ids,
                         self.component | self.meals)
        self._send(self.component)
        self.assertEqual(self.ot.state, 'submitted')
        self.assertTrue(self.ot.handed_over_date)
        self.assertEqual(self.meals_batch.state, 'draft')
        self.assertEqual(self.submission.state, 'draft')
        # Meals is still his to type in.
        self._entry(self.meals_batch, self.emp_a2, user=self.sup_a)

    def test_51_the_gm_has_it_in_front_of_him(self):
        self._send(self.component)
        self.submission.invalidate_recordset()
        self.assertEqual(self.submission.pending_entry_ids,
                         self.ot_a | self.ot_a2)
        self.submission.with_user(self.gm).action_approve()
        self.assertEqual(self.ot.state, 'approved')
        self.assertEqual(self.meals_batch.state, 'draft')
        self.assertNotIn(self.run.state, ('approved', 'paid'))

    def test_52_the_last_component_does_not_hand_the_department_over(self):
        """Only "Every component" says the month is finished: approving a
        handed-over department can finalise and lock the whole month."""
        self._send(self.component)
        self._send(self.meals)
        self.assertEqual(self.submission.state, 'draft')
        self.submission.with_user(self.gm).action_approve()
        self.assertNotIn(self.run.state, ('approved', 'paid'))
        # New work for the month can still be recorded.
        self._batch(self.sup_a, self.dept_a, component=self.env.ref(
            'KSW_commissions.pay_component_friday'))

    def test_53_he_may_take_it_back_before_the_gm_acts(self):
        self._send(self.component)
        self.ot.with_user(self.sup_a).action_reset_to_draft()
        self.assertEqual(self.ot.state, 'draft')
        self.assertFalse(self.ot.handed_over_date)
        self.submission.invalidate_recordset()
        self.assertFalse(self.submission.pending_entry_ids)

    def test_54_returning_one_employee_keeps_the_rest_in_front_of_the_gm(self):
        self._send(self.component)
        self._review(self.submission, 'return', self.emp_a,
                     approve_rest=True)
        self.assertEqual(self.ot.state, 'draft')
        self.assertEqual(self.ot_a2.state, 'approved')
        self.ot_a.with_user(self.sup_a).write({'quantity': 1.0})

    def test_55_nothing_ticked_is_refused(self):
        wizard = self._wizard(self.run)
        wizard.mode = 'some'
        with self.assertRaises(UserError):
            wizard.action_confirm()

    def test_56_an_empty_batch_no_longer_blocks_the_handover(self):
        """KSWCO-shaped: an empty Driver Trips batch beside a filled one."""
        empty = self._batch(self.sup_a, self.dept_a,
                            component=self.env.ref(
                                'KSW_commissions.pay_component_friday'))
        self.assertNotIn(empty.component_id,
                         self._wizard(self.run).allowed_component_ids)
        self._wizard(self.run).action_confirm()
        self.assertEqual(self.submission.state, 'submitted')
        self.assertFalse(empty.exists())

    def test_58_a_batch_submitted_on_its_own_can_still_be_sent(self):
        """KSWCO shape (Sep 2026, 6 batches): the supervisor pressed the
        batch's own Submit ("finished typing") and did not hand over the
        department. That component must still be offered, and sending it
        puts it in front of the GM."""
        self.ot.with_user(self.sup_a).action_submit()
        self.submission.invalidate_recordset()
        self.assertFalse(self.submission.pending_entry_ids)
        self.assertIn(self.component,
                      self._wizard(self.run).allowed_component_ids)
        self._send(self.component)
        self.assertTrue(self.ot.handed_over_date)
        self.submission.invalidate_recordset()
        self.assertEqual(self.submission.pending_entry_ids,
                         self.ot_a | self.ot_a2)
        # And once it is with the GM, it is not offered again.
        self.assertNotIn(self.component,
                         self._wizard(self.run).allowed_component_ids)

    def test_57_a_component_already_in_a_sub_batch_is_not_offered(self):
        """KSWCO-shaped (PB016633): every row went early and was approved.
        There is nothing to send, and sending it only closed the batch."""
        sub = self._sub_batch(self.emp_a | self.emp_a2)
        sub.with_user(self.sup_a).action_submit()
        sub.with_user(self.gm).action_approve()
        self.assertEqual(self.ot.state, 'draft')
        self.assertNotIn(self.component,
                         self._wizard(self.run).allowed_component_ids)
        # Sent anyway (RPC): refused, and the batch stays open.
        with self.assertRaises(UserError):
            self.ot.with_user(self.sup_a).action_hand_over()
        self.assertEqual(self.ot.state, 'draft')
        self._entry(self.ot, self.emp_a, user=self.sup_a)


class TestGmPartialReturn(SubBatchCommon):

    def test_30_refuse_one_employee_approve_the_rest(self):
        """On a normal, whole-department handover."""
        self.submission.with_user(self.sup_a).action_submit()
        self._review(self.submission, 'return', self.emp_a,
                     approve_rest=True)
        # emp_a's overtime and meals come back, with the reason…
        self.assertEqual((self.ot_a | self.meal_a).mapped('state'),
                         ['draft', 'draft'])
        self.assertEqual(self.ot_a.x_return_reason, 'Wrong hours')
        # …the rest is approved and locked.
        self.assertEqual(self.ot_a2.state, 'approved')
        self.assertEqual(self.submission.state, 'returned')
        self.assertEqual(self.ot.state, 'draft')
        # The supervisor fixes the refused row and nothing else.
        self.ot_a.with_user(self.sup_a).write({'quantity': 2.0})
        with self.assertRaises(UserError):
            self.ot_a2.with_user(self.sup_a).write({'quantity': 2.0})
        # Resubmit, approve, done.
        self.submission.with_user(self.sup_a).action_submit()
        self.submission.with_user(self.gm).action_approve()
        self.assertEqual(self.submission.state, 'approved')

    def test_31_refuse_one_pay_type_of_one_employee(self):
        self.submission.with_user(self.sup_a).action_submit()
        lines = self._lines(self.submission)
        # One line per component per employee: 3 here.
        self.assertEqual(len(lines), 3)
        self._send_back(lines.filtered(lambda l: l.entry_ids == self.meal_a),
                        'No lunch that day')
        self.assertEqual(self.meal_a.state, 'draft')
        self.assertEqual(self.ot_a.state, 'submitted')
        # The decided line leaves the screen; the rest are still there.
        self.assertEqual(len(lines.exists()), 2)

    def test_32_a_sub_batch_can_be_partly_refused(self):
        sub = self._sub_batch(self.emp_a | self.emp_a2)
        sub.with_user(self.sup_a).action_submit()
        self._review(sub, 'return', self.emp_a2, approve_rest=True)
        self.assertEqual(self.ot_a2.state, 'draft')
        self.assertEqual((self.ot_a | self.meal_a).mapped('state'),
                         ['approved', 'approved'])
        self.assertEqual(sub.state, 'returned')


class TestGmReviewByComponent(SubBatchCommon):
    """The GM goes component by component and approves or returns some
    employees in each; what he does not touch stays waiting on him."""

    def _pick(self, lines, component, employees):
        # exists(): a decided line leaves the screen, as it does on reload.
        return lines.exists().filtered(
            lambda l: l.component_id == component
            and l.employee_name in employees.mapped('name'))

    def test_60_one_line_per_component_per_employee(self):
        self.submission.with_user(self.sup_a).action_submit()
        lines = self._lines(self.submission)
        overtime = lines.filtered(lambda l: l.component_id == self.component)
        self.assertEqual(len(overtime), 2)
        self.assertEqual(overtime.entry_ids, self.ot_a | self.ot_a2)

    def test_61_approve_one_employee_of_a_component(self):
        self.submission.with_user(self.sup_a).action_submit()
        lines = self._lines(self.submission)
        self._pick(lines, self.component, self.emp_a).action_approve()
        self.assertEqual(self.ot_a.state, 'approved')
        # Not touched: still waiting on him, no reason, still locked.
        self.assertEqual(self.ot_a2.state, 'submitted')
        self.assertFalse(self.ot_a2.x_return_reason)
        self.assertEqual(self.meal_a.state, 'submitted')
        self.assertEqual(self.submission.state, 'submitted')
        self.submission.invalidate_recordset()
        self.assertEqual(self.submission.pending_entry_ids,
                         self.ot_a2 | self.meal_a)
        self.assertNotIn(self.run.state, ('approved', 'paid'))
        with self.assertRaises(UserError):
            self.ot_a2.with_user(self.sup_a).write({'quantity': 1.0})

    def test_62_mixed_decisions_across_components(self):
        """Approve A's overtime, return A2's overtime, approve A's meals —
        in one sitting, component by component."""
        self.submission.with_user(self.sup_a).action_submit()
        lines = self._lines(self.submission)
        self._pick(lines, self.component, self.emp_a).action_approve()
        self._send_back(self._pick(lines, self.component, self.emp_a2),
                        'Hours do not match the sheet')
        self._pick(lines, self.meals, self.emp_a).action_approve()
        self.assertEqual(self.ot_a.state, 'approved')
        self.assertEqual(self.ot_a2.state, 'draft')
        self.assertEqual(self.ot_a2.x_return_reason,
                         'Hours do not match the sheet')
        self.assertEqual(self.meal_a.state, 'approved')
        self.assertFalse(lines.exists())
        self.assertEqual(self.submission.state, 'returned')
        # The supervisor fixes only the returned row, and resubmits.
        self.ot_a2.with_user(self.sup_a).write({'quantity': 2.0})
        self.submission.with_user(self.sup_a).action_submit()
        self.submission.with_user(self.gm).action_approve()
        self.assertEqual(self.submission.state, 'approved')

    def test_63_works_on_a_sub_batch(self):
        sub = self._sub_batch(self.emp_a)
        sub.with_user(self.sup_a).action_submit()
        lines = self._lines(sub)
        self._pick(lines, self.meals, self.emp_a).action_approve()
        self.assertEqual(self.meal_a.state, 'approved')
        self.assertEqual(self.ot_a.state, 'submitted')
        self.assertEqual(sub.state, 'submitted')

    def test_64_a_return_needs_a_reason(self):
        self.submission.with_user(self.sup_a).action_submit()
        lines = self._lines(self.submission)
        with self.assertRaises(UserError):
            self._send_back(lines[:1], '  ')

    def test_65_only_the_departments_gm(self):
        self.submission.with_user(self.sup_a).action_submit()
        with self.assertRaises(UserError):
            self.submission.with_user(self.sup_a).action_open_review()


class TestNothingAfterApproval(SubBatchCommon):
    """What the user asked to be sure of: a normal handover, once approved,
    takes nothing more; a finalised month takes nothing from anyone."""

    def _approved_normally(self):
        # B keeps typing, so A's approval does not finalise the month.
        other = self._batch(self.sup_b, self.dept_b)
        self._entry(other, self.emp_b, user=self.sup_b)
        self.submission.with_user(self.sup_a).action_submit()
        self.submission.with_user(self.gm).action_approve()
        self.assertEqual(self.submission.state, 'approved')
        self.assertNotIn(self.run.state, ('approved', 'paid'))
        return other

    def test_70_no_new_row_no_new_component(self):
        self._approved_normally()
        with self.assertRaises(UserError):
            self._entry(self.ot, self.emp_a, user=self.sup_a)
        with self.assertRaises(UserError):
            self._batch(self.sup_a, self.dept_a, component=self.env.ref(
                'KSW_commissions.pay_component_friday'))

    def test_71_a_returned_department_is_open_again(self):
        self.submission.with_user(self.sup_a).action_submit()
        self.submission.sudo().return_reason = 'Add the Friday allowance'
        self.submission.with_user(self.gm).action_return()
        self._batch(self.sup_a, self.dept_a, component=self.env.ref(
            'KSW_commissions.pay_component_friday'))

    def test_72_a_finalised_month_takes_nothing_from_anyone(self):
        other = self._approved_normally()
        other.submission_id.with_user(self.sup_b).action_submit()
        other.submission_id.with_user(self.gm).action_approve()
        self.assertEqual(self.run.state, 'approved')
        admin = self._user('p_admin72',
                           'KSW_commissions.group_commission_officer')
        for user in (self.sup_a, admin):
            with self.assertRaises(UserError):
                self._entry(self.ot, self.emp_a, user=user)
            with self.assertRaises(UserError):
                self._batch(user, self.dept_a, component=self.env.ref(
                    'KSW_commissions.pay_component_friday'))
        # The GM too — the month is reopened first, not a batch inside it.
        self.assertFalse(self.ot.with_user(self.gm).x_can_reopen)
        with self.assertRaises(UserError):
            self.ot.with_user(self.gm).action_reset_to_draft()
        with self.assertRaises(UserError):
            self.env['ksw.pay.gm.review.wizard'].with_user(
                self.gm)._open_for(self.submission, 'reopen')
        self.assertEqual(self.ot.state, 'approved')


class TestPaidIsFinal(SubBatchCommon):

    def _approved_early(self):
        sub = self._sub_batch(self.emp_a)
        sub.with_user(self.sup_a).action_submit()
        sub.with_user(self.gm).action_approve()
        return sub

    def test_40_the_gm_may_reopen_before_payment(self):
        sub = self._approved_early()
        self._review(sub, 'reopen', self.emp_a, reason='Recount the hours')
        self.assertEqual(self.ot_a.state, 'draft')
        self.assertEqual(sub.state, 'returned')
        self.ot_a.with_user(self.sup_a).write({'quantity': 3.0})

    def test_41_early_approved_rows_are_paid_by_the_month(self):
        """Even though the rest of the department was never submitted."""
        self._approved_early()
        self.run.sudo().action_close_month()
        self.assertEqual(self.run.state, 'approved')
        line = self.run.line_ids.filtered(
            lambda l: l.employee_id == self.emp_a)
        self.assertTrue(line)
        self.assertFalse(self.run.line_ids.filtered(
            lambda l: l.employee_id == self.emp_a2))

    def test_42_a_paid_month_is_reopened_by_nobody(self):
        self._approved_early()
        self.run.sudo().action_close_month()
        self.run.sudo().action_mark_paid()
        with self.assertRaises(UserError):
            self.run.sudo().action_reopen()
        with self.assertRaises(UserError):
            (self.ot_a | self.meal_a)._wf_reopen('too late')
        self.assertEqual(self.ot_a.state, 'approved')

    def test_43_reopening_the_month_keeps_early_approvals(self):
        self._approved_early()
        self.submission.with_user(self.sup_a).action_submit()
        self.submission.with_user(self.gm).action_approve()
        self.assertEqual(self.run.state, 'approved')
        self.run.sudo().action_reopen()
        # The department's own approval steps back; the early one stands.
        self.assertEqual(self.ot_a2.state, 'submitted')
        self.assertEqual(self.ot_a.state, 'approved')
