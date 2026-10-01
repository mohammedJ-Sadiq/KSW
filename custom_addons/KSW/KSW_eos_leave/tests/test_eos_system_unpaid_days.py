"""System-counted unpaid days on the EOS request.

The EOS service period excludes the unpaid days the system already records
(the same rule the annual leave balance uses), plus any Extra Unpaid Days HR
types in for days the system does not know about.
"""
from datetime import date

from odoo.tests.common import TransactionCase


class TestEosSystemUnpaidDays(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.joining = date(2020, 1, 1)
        cls.termination = date(2027, 6, 1)

        cls.employee = cls.env['hr.employee'].create({
            'name': 'EOS Unpaid Days Employee',
            'tz': 'Asia/Riyadh',
        })
        cls.employee.current_version_id.write({
            'date_version': cls.joining,
            'contract_date_start': cls.joining,
            'wage': 6000.0,
            'struct_id': cls.env.ref('om_hr_payroll.structure_base').id,
        })
        cls.employee._compute_current_version_id()

        cls.eos_type = cls.env['hr.leave.type'].create({
            'name': 'EOS (unpaid days test)',
            'leave_validation_type': 'annual_multi',
            'requires_allocation': False,
            'is_eos_leave': True,
            'time_type': 'leave',
        })
        # Needs an approval, so a fresh request stays in `confirm` until the
        # test validates it explicitly.
        cls.unpaid_type = cls.env['hr.leave.type'].create({
            'name': 'Unpaid (EOS unpaid days test)',
            'requires_allocation': False,
            'leave_validation_type': 'manager',
            'request_unit': 'day',
            'is_unpaid_leave': True,
        })

    def _leave(self, leave_type, date_from, date_to):
        return self.env['hr.leave'].with_context(
            tracking_disable=True, mail_create_nosubscribe=True,
        ).create({
            'employee_id': self.employee.id,
            'holiday_status_id': leave_type.id,
            'request_date_from': date_from,
            'request_date_to': date_to,
        })

    def _validated_unpaid(self, date_from, date_to):
        leave = self._leave(self.unpaid_type, date_from, date_to)
        leave.with_context(leave_skip_state_check=True).write(
            {'state': 'validate'})
        return leave

    def _eos(self):
        return self._leave(self.eos_type, self.termination, self.termination)

    def test_validated_unpaid_leave_is_counted(self):
        unpaid = self._validated_unpaid(date(2024, 3, 1), date(2024, 3, 10))
        eos = self._eos()
        self.assertEqual(unpaid.number_of_days, 10.0)
        self.assertAlmostEqual(eos.x_eos_system_unpaid_days, 10.0, places=2)

    def test_unvalidated_and_post_termination_leaves_ignored(self):
        self._leave(self.unpaid_type, date(2024, 3, 1), date(2024, 3, 5))
        self._validated_unpaid(date(2027, 7, 1), date(2027, 7, 5))
        eos = self._eos()
        self.assertEqual(eos.x_eos_system_unpaid_days, 0.0)

    def test_system_and_extra_days_both_reduce_service(self):
        self._validated_unpaid(date(2024, 3, 1), date(2024, 3, 10))
        eos = self._eos()
        eos.sudo().write({'x_eos_unpaid_days': 20.0})
        total_days = (self.termination - self.joining).days
        self.assertAlmostEqual(
            eos.x_eos_adjusted_service_years,
            (total_days - 10.0 - 20.0) / 365.25, places=2)

    def test_view_leaves_action_lists_the_counted_leaves(self):
        unpaid = self._validated_unpaid(date(2024, 3, 1), date(2024, 3, 10))
        eos = self._eos()
        action = eos.action_view_eos_unpaid_leaves()
        listed = self.env['hr.leave'].search(action['domain'])
        self.assertEqual(listed, unpaid)
        self.assertAlmostEqual(
            listed.x_eos_unpaid_days_counted, 10.0, places=2)

    def test_matches_annual_balance_helper(self):
        """EOS and the annual accrual exclude exactly the same days."""
        self._validated_unpaid(date(2024, 3, 1), date(2024, 3, 10))
        eos = self._eos()
        self.assertAlmostEqual(
            eos.x_eos_system_unpaid_days,
            self.env['ksw.annual.leave']._get_unpaid_leave_days(
                self.employee.id, since_date=self.joining),
            places=2)
