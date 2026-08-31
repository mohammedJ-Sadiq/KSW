"""A punch is proof the employee is back — so tell the employee.

Replaces test_return_reminder.py. The old design chased the direct manager
daily until they confirmed a return; it changed nothing, because the manager
is the party already not acting. For a fingerprint employee the system
already has the evidence — a real punch dated on or after the vacation start
while the leave still reads 'On Vacation' — and the employee is the one whose
payslip is blocked by it, so the alert goes to them, once per leave.
"""
from datetime import datetime as dt, timedelta

from odoo import fields
from odoo.tests.common import TransactionCase


class TestReturnPunchNotification(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _mkuser(name, login):
            return cls.env['res.users'].create({
                'name': name,
                'login': login,
                'email': f'{login}@punchnotify.test',
                'group_ids': [(6, 0, [cls.env.ref('base.group_user').id])],
            })

        cls.user_dm = _mkuser('Punch DM', 'punch_dm')
        cls.user_emp = _mkuser('Punch Emp', 'punch_emp')

        cls.employee = cls.env['hr.employee'].create({
            'name': 'Punch Employee',
            'user_id': cls.user_emp.id,
            'leave_manager_id': cls.user_dm.id,
            'work_email': 'punch_employee@punchnotify.test',
        })

        cls.leave_type = cls.env['hr.leave.type'].create({
            'name': 'Annual Leave Punch Test',
            'requires_allocation': False,
            'leave_validation_type': 'annual_multi',
            'is_annual_leave': True,
        })

        cls.today = fields.Date.context_today(cls.env['hr.leave'])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _leave(self, starts_days_ago=10, ends_days_ago=2,
               return_state='on_vacation'):
        leave = self.env['hr.leave'].sudo().create({
            'employee_id': self.employee.id,
            'holiday_status_id': self.leave_type.id,
            'request_date_from': self.today - timedelta(days=starts_days_ago),
            'request_date_to': self.today - timedelta(days=ends_days_ago),
        })
        leave.sudo().write({
            'state': 'validate',
            'x_return_state': return_state,
        })
        return leave

    def _punch(self, when=None, **kw):
        vals = {
            'employee_id': self.employee.id,
            'check_in': when or dt.combine(
                self.today - timedelta(days=1), dt.min.time()
            ) + timedelta(hours=5),
        }
        vals.update(kw)
        vals.setdefault('check_out', vals['check_in'] + timedelta(hours=8))
        return self.env['hr.attendance'].sudo().create(vals)

    def _notifications(self, leave):
        """Notifications sent about this leave.

        message_notify records do NOT appear in ``leave.message_ids`` (it
        filters out user_notification messages), so asserting against that
        field reads the tracking messages instead and proves nothing.
        """
        return self.env['mail.message'].sudo().search([
            ('model', '=', 'hr.leave'),
            ('res_id', '=', leave.id),
            ('message_type', '=', 'user_notification'),
        ])

    # ------------------------------------------------------------------
    # The alert fires
    # ------------------------------------------------------------------

    def test_punch_notifies_the_employee(self):
        leave = self._leave()
        existing_ids = self._notifications(leave).ids

        self._punch()

        leave.invalidate_recordset()
        new_msgs = self._notifications(leave).filtered(
            lambda m: m.id not in existing_ids)
        self.assertEqual(len(new_msgs), 1, 'The employee must be notified.')
        self.assertIn(self.user_emp.partner_id, new_msgs.partner_ids)
        self.assertTrue(leave.x_return_punch_notified_on)

    def test_the_manager_is_not_the_recipient(self):
        """The whole point of the change: stop chasing the manager."""
        leave = self._leave()
        existing_ids = self._notifications(leave).ids

        self._punch()

        leave.invalidate_recordset()
        new_msgs = self._notifications(leave).filtered(
            lambda m: m.id not in existing_ids)
        self.assertTrue(new_msgs)
        self.assertNotIn(
            self.user_dm.partner_id, new_msgs.partner_ids,
            'The direct manager must not be notified by this alert.',
        )

    def test_punch_on_the_first_vacation_day_still_counts(self):
        """An early return is exactly the case worth catching — and the one
        the old end-date-based reminder never saw."""
        leave = self._leave(starts_days_ago=3, ends_days_ago=-10)

        self._punch(when=dt.combine(
            self.today - timedelta(days=3), dt.min.time()
        ) + timedelta(hours=5))

        leave.invalidate_recordset()
        self.assertTrue(leave.x_return_punch_notified_on)

    # ------------------------------------------------------------------
    # The alert does NOT fire
    # ------------------------------------------------------------------

    def test_second_punch_does_not_re_notify(self):
        """A bulk historical device sync must not spam the employee."""
        leave = self._leave()
        self._punch()
        leave.invalidate_recordset()
        first_stamp = leave.x_return_punch_notified_on
        existing_ids = self._notifications(leave).ids

        self._punch(when=dt.combine(
            self.today, dt.min.time()) + timedelta(hours=5))

        leave.invalidate_recordset()
        self.assertEqual(leave.x_return_punch_notified_on, first_stamp)
        self.assertFalse(self._notifications(leave).filtered(
            lambda m: m.id not in existing_ids))

    def test_confirmed_return_notifies_nobody(self):
        leave = self._leave(return_state='hr_confirmed')
        existing_ids = self._notifications(leave).ids

        self._punch()

        leave.invalidate_recordset()
        self.assertFalse(self._notifications(leave).filtered(
            lambda m: m.id not in existing_ids))
        self.assertFalse(leave.x_return_punch_notified_on)

    def test_punch_before_the_vacation_starts_notifies_nobody(self):
        """Working right up to the vacation is not evidence of a return."""
        leave = self._leave(starts_days_ago=-5, ends_days_ago=-15)

        self._punch()

        leave.invalidate_recordset()
        self.assertFalse(leave.x_return_punch_notified_on)

    def test_absence_rows_are_not_punches(self):
        """Generated absence records are not evidence of anything."""
        leave = self._leave()

        self._punch(x_is_absent=True)

        leave.invalidate_recordset()
        self.assertFalse(leave.x_return_punch_notified_on)

    def test_attendance_sheet_records_are_not_punches(self):
        """Fabricated sheet attendance must not stand in for a fingerprint."""
        leave = self._leave()
        if 'x_is_auto_generated' not in self.env['hr.attendance']._fields:
            self.skipTest('KSW_attendance_sheet is not installed.')

        self._punch(x_is_auto_generated=True)

        leave.invalidate_recordset()
        self.assertFalse(leave.x_return_punch_notified_on)

    def test_another_employees_punch_does_not_notify(self):
        leave = self._leave()
        other = self.env['hr.employee'].create({
            'name': 'Punch Other Employee',
            'user_id': self.env['res.users'].create({
                'name': 'Punch Other',
                'login': 'punch_other',
                'email': 'punch_other@punchnotify.test',
                'group_ids': [(6, 0, [self.env.ref('base.group_user').id])],
            }).id,
        })

        self.env['hr.attendance'].sudo().create({
            'employee_id': other.id,
            'check_in': dt.combine(
                self.today, dt.min.time()) + timedelta(hours=5),
            'check_out': dt.combine(
                self.today, dt.min.time()) + timedelta(hours=13),
        })

        leave.invalidate_recordset()
        self.assertFalse(leave.x_return_punch_notified_on)

    # ------------------------------------------------------------------
    # Email
    # ------------------------------------------------------------------

    def test_an_email_is_queued_for_the_employee(self):
        """message_notify delivers to inbox OR email per the recipient's
        preference, so the email is sent separately — an employee who never
        signs in to Odoo must still hear about it."""
        leave = self._leave()
        before = self.env['mail.mail'].sudo().search_count([])

        self._punch()

        after = self.env['mail.mail'].sudo().search_count([])
        self.assertGreater(
            after, before, 'An email must be queued to the work address.')
