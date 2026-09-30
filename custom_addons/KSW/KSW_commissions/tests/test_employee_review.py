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
