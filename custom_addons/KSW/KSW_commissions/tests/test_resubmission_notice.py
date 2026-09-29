# -*- coding: utf-8 -*-
"""A department that comes back after a return reaches the GM *as* a
resubmission, naming what was reopened, by whom and why — not as another
generic 'Submitted for approval'."""
from .test_submission import SubmissionCommon


class TestResubmissionNotice(SubmissionCommon):

    def _gm_messages(self, submission):
        return submission.message_ids.filtered(
            lambda m: self.gm.partner_id in m.partner_ids)

    def test_resubmission_after_gm_return_is_flagged(self):
        batch = self._filled_batch(self.sup_a, self.dept_a, self.emp_a)
        submission = batch.submission_id
        submission.with_user(self.sup_a).action_submit()
        first = self._gm_messages(submission)
        self.assertIn('Submitted for approval', first[:1].body)

        submission.with_user(self.gm).write({'return_reason': 'Fix the hours'})
        submission.with_user(self.gm).action_return()
        batch.entry_ids.with_user(self.sup_a).write({'quantity': 2.0})
        submission.with_user(self.sup_a).action_submit()

        latest = (self._gm_messages(submission) - first)[:1]
        self.assertIn('Resubmitted after changes', latest.body)
        self.assertIn('Fix the hours', latest.body)
        self.assertIn(batch.name, latest.body)

    def test_first_submission_is_not_a_resubmission(self):
        batch = self._filled_batch(self.sup_a, self.dept_a, self.emp_a)
        batch.submission_id.with_user(self.sup_a).action_submit()
        body = self._gm_messages(batch.submission_id)[:1].body
        self.assertNotIn('Resubmitted', body)
