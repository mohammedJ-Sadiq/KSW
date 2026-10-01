"""The per-employee review page behind a department's Employees button.

Two complaints shaped it: the quantity total added overtime hours to meal
counts, and moving to the next employee meant going back to the list.
"""
from .test_submission import SubmissionCommon


class TestEmployeeReview(SubmissionCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.emp_a2 = cls._employee('Sub Emp A2', cls.dept_a, 6000.0)

    def _fill(self):
        overtime = self._batch(self.sup_a, self.dept_a)
        self._entry(overtime, self.emp_a, user=self.sup_a, quantity=4.0)
        self._entry(overtime, self.emp_a, user=self.sup_a, quantity=3.5)
        self._entry(overtime, self.emp_a2, user=self.sup_a, quantity=2.0)
        meals = self._batch(self.sup_a, self.dept_a, component=self.meals)
        self._entry(meals, self.emp_a, user=self.sup_a, quantity=5.0)
        self.env.flush_all()
        return overtime.submission_id

    def _review(self, submission, user, employee):
        action = submission.with_user(user).action_open_entries()
        self.assertEqual(action['res_model'], 'ksw.pay.employee.review')
        return self.env[action['res_model']].with_user(user).search(
            action['domain'] + [('employee_id', '=', employee.id)])

    def test_01_one_row_per_employee_so_the_pager_can_walk_them(self):
        submission = self._fill()
        action = submission.with_user(self.sup_a).action_open_entries()
        rows = self.env[action['res_model']].with_user(self.sup_a).search(
            action['domain'])
        self.assertEqual(rows.employee_id, self.emp_a | self.emp_a2)

    def test_02_each_component_is_its_own_section_with_its_own_subtotal(self):
        submission = self._fill()
        review = self._review(submission, self.sup_a, self.emp_a)
        self.assertEqual(review.entry_count, 3)
        html = str(review.sections_html)
        # 4 + 3.5 hours of overtime, and 5 meals — never "12.5" of anything.
        self.assertIn('7.5', html)
        self.assertIn('Hours', html)
        self.assertIn('Meals', html)
        self.assertNotIn('12.5', html)
        self.assertEqual(
            review.amount,
            sum(review.entry_ids.mapped('amount')))

    def test_03_another_supervisor_sees_no_row(self):
        submission = self._fill()
        rows = self.env['ksw.pay.employee.review'].with_user(
            self.sup_b).search([('submission_id', '=', submission.id)])
        self.assertFalse(rows)

    def test_04_the_gm_reviews_the_same_page(self):
        submission = self._fill()
        review = self._review(submission, self.gm, self.emp_a)
        self.assertEqual(review.entry_count, 3)


class TestWhoGetsPaidReview(SubmissionCommon):
    """The run's Who Gets Paid tab, one employee at a time."""

    def _handed_over_run(self):
        a = self._batch(self.sup_a, self.dept_a)
        self._entry(a, self.emp_a, user=self.sup_a, quantity=4.0)
        self._entry(a, self.emp_a, user=self.sup_a, quantity=3.5)
        meals = self._batch(self.sup_a, self.dept_a, component=self.meals)
        self._entry(meals, self.emp_a, user=self.sup_a, quantity=5.0)
        b = self._filled_batch(self.sup_b, self.dept_b, self.emp_b)
        a.submission_id.with_user(self.sup_a).action_submit()
        b.submission_id.with_user(self.sup_b).action_submit()
        return a.submission_id.run_id

    def test_01_sections_add_up_to_the_earnings(self):
        run = self._handed_over_run()
        line = run.line_ids.filtered(lambda l: l.employee_id == self.emp_a)
        html = str(line.with_user(self.gm).x_review_sections_html)
        self.assertIn('7.5', html)
        self.assertIn('Hours', html)
        self.assertIn('Meals', html)
        self.assertNotIn('12.5', html)
        self.assertIn('Total, all components', html)
        # The page's grand total is the line's own Earnings.
        from odoo.tools import format_amount
        self.assertIn(format_amount(self.env, line.earnings, line.currency_id),
                      html)

    def _session(self, run, user):
        action = run.with_user(user).action_review_employees()
        self.assertEqual(action['target'], 'new')
        self.assertEqual(action['res_model'], 'ksw.pay.run.review')
        return self.env['ksw.pay.run.review'].with_user(user).browse(
            action['res_id'])

    def test_02_next_and_previous_walk_the_gms_list(self):
        run = self._handed_over_run()
        review = self._session(run, self.gm)
        review.sort_by = 'name'
        review.line_id = review._ordered_lines()[:1]
        self.assertEqual(review.position, '1 / 2')
        self.assertFalse(review.has_prev)
        self.assertTrue(review.has_next)
        first = review.line_id
        action = review.action_next()
        self.assertEqual(action['res_id'], review.id, 'same dialog')
        self.assertNotEqual(review.line_id, first)
        self.assertEqual(review.position, '2 / 2')
        self.assertFalse(review.has_next)
        review.action_prev()
        self.assertEqual(review.line_id, first)

    def test_02b_sort_and_filter_change_what_next_walks(self):
        run = self._handed_over_run()
        review = self._session(run, self.gm)
        # Highest earnings first, then narrowed to department B only.
        review.sort_by = 'earnings_desc'
        ordered = review._ordered_lines()
        self.assertGreaterEqual(ordered[0].earnings, ordered[1].earnings)
        review.department_ids = self.dept_b
        self.assertEqual(review._ordered_lines().employee_id, self.emp_b)
        review.department_ids = False
        review.search_text = 'emp a'
        self.assertEqual(review._ordered_lines().employee_id, self.emp_a)
        review.search_text = 'nobody'
        review._onchange_order()
        self.assertFalse(review.line_id)
        self.assertEqual(review.position, '0 / 0')

    def test_02e_the_dialog_filter_and_sort_as_the_client_runs_them(self):
        """Through Form, so the bar runs as an onchange on NewId records:
        a department filter that emptied the list, and a sort that kept the
        same employee on screen, both passed the plain-ORM tests above."""
        from odoo.tests import Form
        run = self._handed_over_run()
        review = self._session(run, self.gm)
        with Form(review) as f:
            f.department_ids.add(self.dept_b)
            self.assertEqual(f.line_id.employee_id, self.emp_b)
            self.assertEqual(f.position, '1 / 1')
            f.department_ids.clear()
            f.sort_by = 'name'
            first_by_name = f.line_id
            f.sort_by = 'earnings_desc'
            top = review._ordered_lines()[:1]
            self.assertEqual(f.line_id, top, 'a new sort starts at its top')
            self.assertEqual(f.position, '1 / 2')
        self.assertTrue(first_by_name)

    def test_02c_the_last_sort_is_remembered(self):
        run = self._handed_over_run()
        self._session(run, self.gm).write({'sort_by': 'name'})
        self.assertEqual(self._session(run, self.gm).sort_by, 'name')
        # Another reviewer keeps the default.
        self.assertEqual(self._session(run, self.sup_a).sort_by, 'department')

    def test_02d_review_from_a_row_starts_on_that_employee(self):
        run = self._handed_over_run()
        line = run.line_ids.filtered(lambda l: l.employee_id == self.emp_b)
        action = line.with_user(self.gm).action_open_review()
        review = self.env['ksw.pay.run.review'].browse(action['res_id'])
        self.assertEqual(review.line_id, line)

    def test_03_a_supervisor_walks_only_his_people(self):
        run = self._handed_over_run()
        review = self._session(run, self.sup_a)
        self.assertEqual(review.line_id.employee_id, self.emp_a)
        self.assertEqual(review.position, '1 / 1')
        self.assertFalse(review.allowed_department_ids - self.dept_a)

    def test_04_a_draft_entry_is_listed_apart(self):
        run = self._handed_over_run()
        # Another department's batch, still being typed: not handed over.
        late = self.env['ksw.pay.batch'].sudo().create({
            'component_id': self.meals.id,
            'department_id': self.dept_b.id, 'period': self.period})
        self._entry(late, self.emp_a, quantity=1.0)
        line = run.line_ids.filtered(lambda l: l.employee_id == self.emp_a)
        html = str(line.x_review_sections_html)
        self.assertIn('Not in this payment', html)
        self.assertIn('Not handed over to the GM yet', html)
