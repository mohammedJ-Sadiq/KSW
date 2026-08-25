"""Daily reminder chasing the direct manager to confirm a vacation return.

An unconfirmed return blocks the employee's monthly payslip, and nothing used
to tell the manager the employee was due back.  The cron reminds them once a
day until they confirm — and never reminds them at all once they have, which
includes confirming an *early* return before the expected date (the case that
prompted this: a return confirmed Jul 28 for a vacation planned to Aug 7 must
not produce a reminder on Aug 8).
"""
from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase


class TestReturnReminder(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _mkuser(name, login, extra_groups=()):
            group_ids = [cls.env.ref('base.group_user').id]
            group_ids.extend(extra_groups)
            return cls.env['res.users'].create({
                'name': name,
                'login': login,
                'email': f'{login}@retremind.test',
                'group_ids': [(6, 0, group_ids)],
            })

        cls.user_dm = _mkuser('Remind DM', 'remind_dm')
        cls.user_hr = _mkuser(
            'Remind HR', 'remind_hr',
            [cls.env.ref('KSW_annual_leave.group_annual_leave_hr').id])

        cls.employee = cls.env['hr.employee'].create({
            'name': 'Reminder Employee',
            'user_id': _mkuser('Remind Emp', 'remind_emp').id,
            'leave_manager_id': cls.user_dm.id,
        })
        cls.orphan = cls.env['hr.employee'].create({
            'name': 'Reminder Employee Without Manager',
            'user_id': _mkuser('Remind Orphan', 'remind_orphan').id,
        })

        cls.leave_type = cls.env['hr.leave.type'].create({
            'name': 'Annual Leave Reminder Test',
            'requires_allocation': False,
            'leave_validation_type': 'annual_multi',
            'is_annual_leave': True,
        })

        cls.today = fields.Date.context_today(cls.env['hr.leave'])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _leave(self, ends_days_ago, employee=None, return_state='on_vacation'):
        """A validated annual leave whose planned return is N days past.

        ends_days_ago=1 means request_date_to was yesterday, i.e. the employee
        was expected back today (overdue = 0).
        """
        employee = employee or self.employee
        date_to = self.today - timedelta(days=ends_days_ago)
        leave = self.env['hr.leave'].sudo().create({
            'employee_id': employee.id,
            'holiday_status_id': self.leave_type.id,
            'request_date_from': date_to - timedelta(days=6),
            'request_date_to': date_to,
        })
        leave.sudo().write({
            'state': 'validate',
            'x_annual_approval_state': 'approved',
            'x_return_state': return_state,
        })
        return leave

    def _run(self):
        return self.env['hr.leave']._cron_return_confirmation_reminders(
            commit=False)

    @staticmethod
    def _reminders(leave, existing_ids=()):
        return leave.message_ids.filtered(
            lambda m: m.id not in existing_ids
            and 'Return Confirmation Pending' in (m.body or '')
        )

    # ==================================================================
    # When it fires
    # ==================================================================

    def test_reminds_manager_on_the_expected_return_day(self):
        leave = self._leave(ends_days_ago=1)

        self._run()

        msgs = self._reminders(leave)
        self.assertEqual(len(msgs), 1)
        self.assertIn(self.user_dm.partner_id, msgs.partner_ids)

    def test_stamps_the_leave(self):
        leave = self._leave(ends_days_ago=1)

        self._run()

        self.assertEqual(leave.x_return_reminder_last_sent, self.today)
        self.assertEqual(leave.x_return_reminder_count, 1)

    def test_only_one_reminder_per_day(self):
        leave = self._leave(ends_days_ago=1)

        self._run()
        self._run()

        self.assertEqual(len(self._reminders(leave)), 1)
        self.assertEqual(leave.x_return_reminder_count, 1)

    def test_reminds_again_the_next_day(self):
        leave = self._leave(ends_days_ago=3)
        self._run()
        existing_ids = leave.message_ids.ids
        leave.sudo().write({
            'x_return_reminder_last_sent': self.today - timedelta(days=1)})

        self._run()

        self.assertEqual(len(self._reminders(leave, existing_ids)), 1)
        self.assertEqual(leave.x_return_reminder_count, 2)

    # ==================================================================
    # When it must stay quiet
    # ==================================================================

    def test_no_reminder_before_the_expected_return(self):
        """The employee is still on vacation — nothing to confirm yet."""
        leave = self._leave(ends_days_ago=-3)   # ends in 3 days

        self._run()

        self.assertFalse(self._reminders(leave))
        self.assertFalse(leave.x_return_reminder_last_sent)

    def test_no_reminder_once_the_return_is_confirmed(self):
        """The early-confirmation case: confirmed before the planned date."""
        leave = self._leave(ends_days_ago=1)
        leave.sudo().write({
            'x_return_date': self.today - timedelta(days=5)})
        leave.with_user(self.user_dm).sudo().action_confirm_return_manager()
        existing_ids = leave.message_ids.ids

        self._run()

        self.assertEqual(leave.x_return_state, 'hr_confirmed')
        self.assertFalse(self._reminders(leave, existing_ids))

    def test_no_reminder_for_a_non_annual_leave(self):
        other_type = self.env['hr.leave.type'].create({
            'name': 'Sick Leave Reminder Test',
            'requires_allocation': False,
            'is_annual_leave': False,
        })
        date_to = self.today - timedelta(days=2)
        leave = self.env['hr.leave'].sudo().create({
            'employee_id': self.employee.id,
            'holiday_status_id': other_type.id,
            'request_date_from': date_to - timedelta(days=3),
            'request_date_to': date_to,
        })
        leave.sudo().write({
            'state': 'validate', 'x_return_state': 'on_vacation'})

        self._run()

        self.assertFalse(self._reminders(leave))

    def test_no_reminder_for_an_unvalidated_leave(self):
        leave = self._leave(ends_days_ago=2)
        leave.sudo().write({'state': 'confirm'})

        self._run()

        self.assertFalse(self._reminders(leave))

    # ==================================================================
    # Escalation to HR
    # ==================================================================

    def test_hr_is_not_copied_before_the_escalation_window(self):
        leave = self._leave(ends_days_ago=4)   # overdue = 3

        self._run()

        msgs = self._reminders(leave)
        self.assertIn(self.user_dm.partner_id, msgs.partner_ids)
        self.assertNotIn(self.user_hr.partner_id, msgs.partner_ids)

    def test_hr_is_copied_after_seven_days(self):
        leave = self._leave(ends_days_ago=8)   # overdue = 7

        self._run()

        msgs = self._reminders(leave)
        self.assertIn(self.user_dm.partner_id, msgs.partner_ids)
        self.assertIn(self.user_hr.partner_id, msgs.partner_ids)

    def test_hr_is_told_immediately_when_there_is_no_manager(self):
        leave = self._leave(ends_days_ago=1, employee=self.orphan)

        self._run()

        msgs = self._reminders(leave)
        self.assertEqual(len(msgs), 1)
        self.assertIn(self.user_hr.partner_id, msgs.partner_ids)
        self.assertIn('no Direct Manager', msgs.body)
