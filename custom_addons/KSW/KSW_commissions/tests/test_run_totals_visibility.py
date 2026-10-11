"""Company-wide pay run totals are not a supervisor's to see.

Reported on KSWCO (Oct 2026): a supervisor opening Monthly Pay Run saw the
Total Earnings and Total Payable of every department. Only the General
Manager, the accountant, the commission Administrator and the Settings
administrator see them; a supervisor keeps his own team's subtotal on the
rule-scoped register.
"""
from odoo.exceptions import AccessError

from .test_submission import SubmissionCommon

TOTALS = ('total_earnings', 'total_loan_offset', 'total_payable')


class TestRunTotalsVisibility(SubmissionCommon):

    def _approved_run(self):
        batch = self._filled_batch(self.sup_a, self.dept_a, self.emp_a)
        batch.submission_id.sudo().action_submit()
        run = batch.submission_id.run_id
        run.with_user(self.gm).action_approve()
        return run

    def test_a_supervisor_cannot_read_the_totals(self):
        run = self._approved_run()
        for field in TOTALS:
            with self.assertRaises(AccessError):
                run.with_user(self.sup_a).read([field])

    def test_a_supervisor_list_has_no_total_columns(self):
        arch = self.env['ksw.pay.run'].with_user(self.sup_a).get_views(
            [(False, 'list')])['views']['list']['arch']
        for field in TOTALS:
            self.assertNotIn(f'name="{field}"', arch)

    def test_the_owning_roles_can_read_them(self):
        run = self._approved_run()
        accountant = self._user(
            'tot_acc', 'KSW_commissions.group_commission_accountant')
        admin = self._user(
            'tot_admin', 'KSW_commissions.group_commission_officer')
        for user in (self.gm, accountant, admin):
            values = run.with_user(user).read(list(TOTALS))[0]
            self.assertAlmostEqual(values['total_earnings'], 180.0, places=2)
            arch = self.env['ksw.pay.run'].with_user(user).get_views(
                [(False, 'list')])['views']['list']['arch']
            self.assertIn('name="total_payable"', arch)

    def test_the_finalise_note_carries_no_amounts(self):
        """The chatter is readable by every supervisor of the month."""
        run = self._approved_run()
        existing = run.message_ids.ids
        run.sudo().action_close_month()
        new = run.message_ids.filtered(lambda m: m.id not in existing)
        self.assertTrue(new)
        for message in new:
            self.assertNotIn('Payable', message.body)
            self.assertNotIn('Earnings', message.body)
