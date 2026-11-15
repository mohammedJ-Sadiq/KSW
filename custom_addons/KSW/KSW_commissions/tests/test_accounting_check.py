"""Accounting checks commissions before the General Manager (Nov 2026).

KSWCO SUB00013: a second sub-batch over the same October days imported two
drivers' trips again beside the rows SUB00008 had approved, the GM approved
it over the "Also in SUB00008" warning, and the accountant only noticed at
Step 4 of their vacation requests — after the GM. Three things changed, and
each has tests here:

* a handover goes to the department's Accounting Approvers first; the GM
  can act only on rows they checked, and a row sent back comes back
  unchecked;
* a sub-batch import skips days another handover already holds;
* the same imported days in two handovers are refused, not just flagged.
"""
from odoo import fields
from odoo.exceptions import UserError

from .test_sub_batch_dates import SendEarlyCommon


class AccountingCheckCommon(SendEarlyCommon):

    def setUp(self):
        super().setUp()
        # Another department's accountant: same group, wrong department.
        self.other_acc = self._user(
            'sub_acc_other', 'KSW_commissions.group_commission_accountant')
        other_emp = self.env['hr.employee'].sudo().create({
            'name': 'Other Acc Emp', 'user_id': self.other_acc.id})
        self.dept_b.sudo().write({'x_accountant_ids': [(6, 0, other_emp.ids)]})

    def _pending(self, model, user):
        return self.env[model].with_user(user).search(
            [('x_is_pending_my_action', '=', True)])


class TestCheckBeforeGm(AccountingCheckCommon):

    def test_01_a_sub_batch_goes_to_accounting_first(self):
        sub = self._send(self.ot, self.emp_a, check=False)
        self.assertEqual(sub.state, 'checking')
        self.assertFalse(self.ot_a.x_acc_checked_by)
        self.assertTrue(sub.with_user(self.accountant).x_can_acc_check)
        self.assertFalse(sub.with_user(self.gm).x_can_review)
        with self.assertRaises(UserError):
            sub.with_user(self.gm).action_approve()
        self.assertEqual(self.ot_a.state, 'submitted')

    def test_02_checked_it_reaches_the_gm(self):
        sub = self._send(self.ot, self.emp_a, check=False)
        sub.with_user(self.accountant).action_acc_check()
        self.assertEqual(sub.state, 'submitted')
        self.assertEqual(self.ot_a.x_acc_checked_by, self.accountant)
        self.assertTrue(sub.with_user(self.gm).x_can_review)
        sub.with_user(self.gm).action_approve()
        self.assertEqual(sub.state, 'approved')
        self.assertEqual(self.ot_a.state, 'approved')

    def test_03_only_this_departments_accountants_check(self):
        sub = self._send(self.ot, self.emp_a, check=False)
        for user in (self.other_acc, self.gm, self.sup_a):
            with self.assertRaises(UserError, msg=user.login):
                sub.with_user(user).action_acc_check()
        self.assertFalse(sub.with_user(self.other_acc).x_can_acc_check)
        self.assertFalse(self.ot_a.x_acc_checked_by)

    def test_04_the_department_handover_goes_to_accounting_first(self):
        self.submission.with_user(self.sup_a).action_submit()
        self.assertEqual(self.submission.state, 'checking')
        with self.assertRaises(UserError):
            self.submission.with_user(self.gm).action_approve()
        self.submission.with_user(self.accountant).action_acc_check()
        self.assertEqual(self.submission.state, 'submitted')
        self.submission.with_user(self.gm).action_approve()
        self.assertEqual(self.submission.state, 'approved')

    def test_05_accounting_returns_and_the_resubmission_comes_back(self):
        sub = self._send(self.ot, self.emp_a, check=False)
        action = self.env['ksw.pay.gm.review.wizard'].with_user(
            self.accountant)._open_for(sub, 'check')
        lines = self.env['ksw.pay.gm.review.line'].with_user(
            self.accountant).search(action['domain'])
        dialog = self.env['ksw.pay.gm.review.reason'].with_user(
            self.accountant).browse(lines.action_return()['res_id'])
        dialog.reason = 'Duplicate of SUB00008'
        dialog.action_confirm()
        self.assertEqual(self.ot_a.state, 'draft')
        self.assertEqual(self.ot_a.x_return_reason, 'Duplicate of SUB00008')
        self.assertEqual(sub.state, 'returned')
        sub.with_user(self.sup_a).action_submit()
        self.assertEqual(sub.state, 'checking')

    def test_06_a_gm_return_comes_back_unchecked(self):
        sub = self._send(self.ot, self.emp_a, check=False)
        sub.with_user(self.accountant).action_acc_check()
        lines = self._lines(sub)
        self._send_back(lines.filtered(
            lambda l: l.employee_name == self.emp_a.name))
        self.assertEqual(self.ot_a.state, 'draft')
        self.assertFalse(self.ot_a.x_acc_checked_by)
        sub.with_user(self.sup_a).action_submit()
        self.assertEqual(sub.state, 'checking')

    def test_07_the_gm_review_screen_lists_only_checked_rows(self):
        sub = self._send(self.ot, self.emp_a, check=False)
        with self.assertRaises(UserError):
            self._lines(sub)
        sub.with_user(self.accountant).action_acc_check()
        self.assertTrue(self._lines(sub))

    def test_08_accounting_cannot_approve_from_its_screen(self):
        sub = self._send(self.ot, self.emp_a, check=False)
        action = self.env['ksw.pay.gm.review.wizard'].with_user(
            self.accountant)._open_for(sub, 'check')
        lines = self.env['ksw.pay.gm.review.line'].with_user(
            self.accountant).search(action['domain'])
        with self.assertRaises(UserError):
            lines.action_approve()
        self.assertEqual(self.ot_a.state, 'submitted')

    def test_09_no_accountant_no_handover(self):
        """A step nobody can clear is refused at the handover, not left to
        hang (the company has no default team in this fixture)."""
        self.dept_a.sudo().x_accountant_ids = [(5, 0, 0)]
        self.env.company.sudo().x_default_accountant_ids = [(5, 0, 0)]
        with self.assertRaises(UserError):
            self.submission.with_user(self.sup_a).action_submit()


class TestWaitingForYou(AccountingCheckCommon):

    def test_10_each_role_sees_its_step_and_stops_after_it(self):
        sub = self._send(self.ot, self.emp_a, check=False)
        Sub, Dept = 'ksw.pay.sub.batch', 'ksw.pay.submission'
        self.assertIn(sub, self._pending(Sub, self.accountant))
        self.assertIn(self.submission, self._pending(Dept, self.accountant))
        self.assertNotIn(sub, self._pending(Sub, self.gm))
        self.assertNotIn(sub, self._pending(Sub, self.other_acc))

        sub.with_user(self.accountant).action_acc_check()
        self.assertNotIn(sub, self._pending(Sub, self.accountant))
        self.assertIn(sub, self._pending(Sub, self.gm))
        self.assertIn(self.submission, self._pending(Dept, self.gm))

        sub.with_user(self.gm).action_approve()
        self.assertNotIn(sub, self._pending(Sub, self.gm))
        self.assertNotIn(self.submission, self._pending(Dept, self.gm))

    def test_11_the_negative_filter_is_the_complement(self):
        sub = self._send(self.ot, self.emp_a, check=False)
        others = self.env['ksw.pay.sub.batch'].with_user(
            self.accountant).search([('x_is_pending_my_action', '=', False)])
        self.assertNotIn(sub, others)


class TestTripsNotImportedTwice(AccountingCheckCommon):
    """KSWCO SUB00013: the second sub-batch over the same days."""

    def _approved(self, employees, date_from, date_to):
        sub = self._send(self.trips_batch, employees, date_from, date_to,
                         check=False)
        sub.with_user(self.accountant).action_acc_check()
        sub.with_user(self.gm).action_approve()
        return sub

    def test_20_a_second_sub_batch_skips_the_days_already_approved(self):
        first = self._approved(self.driver_1, '2029-03-01', '2029-03-31')
        row = self._rows(self.driver_1)
        self.assertEqual(len(row), 1)
        # His meals, say, still to send: a second sub-batch, same days.
        self._entry(self.meals_batch, self.driver_1, user=self.sup_a)
        second = self._send(self.meals_batch, self.driver_1,
                            '2029-03-01', '2029-03-31')
        self.assertEqual(self._rows(self.driver_1), row)
        self.assertEqual(row.x_sub_batch_id, first)
        self.assertFalse(second.entry_ids.filtered(
            lambda e: e.component_id == self.trips))

    def test_21_only_the_days_not_yet_handed_over_are_imported(self):
        self._approved(self.driver_1, '2029-03-01', '2029-03-15')
        self._entry(self.meals_batch, self.driver_1, user=self.sup_a)
        second = self._send(self.meals_batch, self.driver_1,
                            '2029-03-01', '2029-03-31')
        trips = second.entry_ids.filtered(
            lambda e: e.component_id == self.trips)
        self.assertEqual(
            (trips.x_window_from, trips.x_window_to),
            (fields.Date.to_date('2029-03-16'),
             fields.Date.to_date('2029-03-31')))
        self.assertFalse(trips.x_overlap_note)

    def test_22_the_same_imported_days_twice_are_refused(self):
        """Whatever route made it: submit, check and approve all refuse."""
        first = self._approved(self.driver_1, '2029-03-01', '2029-03-31')
        dup = self.env['ksw.pay.entry'].sudo().create({
            'batch_id': self.trips_batch.id,
            'employee_id': self.driver_1.id, 'quantity': 40.0,
            'x_window_from': '2029-03-01', 'x_window_to': '2029-03-31',
        })
        self.assertIn(first.name, dup.x_overlap_note)
        with self.assertRaises(UserError):
            dup._wf_submit()
        self.assertEqual(dup.state, 'draft')

    def test_23_typed_rows_stay_a_warning(self):
        """Meals before a vacation and after the return are both real."""
        sub = self._send(self.meals_batch, self.emp_a, check=False)
        self._acc_check(sub)
        sub.with_user(self.gm).action_approve()
        self._entry(self.meals_batch, self.emp_a, user=self.sup_a)
        second = self._send(self.meals_batch, self.emp_a, check=False)
        self.assertEqual(second.state, 'checking')
        self.assertTrue(second.x_overlap_count)
