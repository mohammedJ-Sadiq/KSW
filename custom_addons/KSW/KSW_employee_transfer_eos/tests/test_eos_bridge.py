from datetime import date

from odoo.addons.KSW_employee_transfer.tests.test_employee_transfer import TestEmployeeTransfer
from odoo.tests import tagged


@tagged('post_install', '-at_install')
class TestEosBridge(TestEmployeeTransfer):
    """A GM end of service opens the End of Service request in Time Off;
    nothing waits in Employee Transfers."""

    def test_end_of_service_opens_the_eos_request(self):
        release = self._release()
        line = release.release_line_ids
        self._decide(line, 'terminate', date=date(2026, 12, 31))  # no note needed
        leave = line.sudo().x_eos_leave_ids
        self.assertEqual(len(leave), 1)
        self.assertTrue(leave.x_is_eos_leave)
        self.assertEqual((leave.request_date_from, leave.request_date_to), (date(2026, 12, 31), date(2026, 12, 31)))
        self.assertEqual(leave.x_eos_termination_reason, '84')
        self.assertEqual(line.status, 'eos')
        self.assertTrue(line.x_eos_progress)
        # Nothing for HR (or anyone) in this app: it continues in Time Off.
        mine = [('x_is_pending_my_action', '=', True)]
        for user in (self.hr, self.gm, self.manager):
            self.assertNotIn(release, self.Request.with_user(user).search(mine))
        self.assertFalse(release.x_waiting_hr)
        # Refused there: back on the GM's list.
        leave.write({'state': 'refuse'})
        self.assertEqual(line.status, 'gm')
        self.assertIn(release, self.Request.with_user(self.gm).search(mine))
        # Decided again and validated in Time Off: done.
        self._decide(line, 'terminate', date=date(2026, 12, 31))
        line.sudo().x_eos_leave_ids.filtered(lambda l: l.state != 'refuse').write({'state': 'validate'})
        self.assertEqual(line.status, 'terminated')
        self.assertEqual(release.state, 'done')
