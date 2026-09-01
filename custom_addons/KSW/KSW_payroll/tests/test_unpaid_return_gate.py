# -*- coding: utf-8 -*-
"""An unconfirmed return blocks the payslip — for unpaid leave too.

`_get_unresolved_vacation_leaves` used to name `is_annual_leave`, so the whole
gate — the `compute_sheet` guard, the batch skip, and the attendance-sheet
blocker that all read it — simply did not see unpaid leave. An employee could
be paid for a month nobody had confirmed he was back from.

The type clause is gone: `x_return_state` only ever leaves 'not_applicable' on
a type that uses the return system, so the state is the filter and any type
adopting it is covered without touching the query again.
"""
from datetime import date

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class TestUnpaidReturnGate(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Unpaid Gate Test Calendar',
            'tz': 'Asia/Riyadh',
        })
        cls.manager_user = cls.env['res.users'].create({
            'name': 'Unpaid Gate Test Manager',
            'login': 'unpaid_gate_dm',
            'email': 'unpaid_gate_dm@example.com',
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id])],
        })
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Unpaid Gate Test Employee',
            'resource_calendar_id': cls.calendar.id,
            'tz': 'Asia/Riyadh',
            'leave_manager_id': cls.manager_user.id,
        })
        cls.version = cls.employee.current_version_id
        cls.version.write({
            'name': 'Unpaid Gate Test Version',
            'date_version': date(2025, 1, 1),
            'contract_date_start': date(2025, 1, 1),
            'resource_calendar_id': cls.calendar.id,
            'wage': 6000.0,
            'da': 0.0,
            'travel_allowance': 500.0,
            'mobile_allowance': 0.0,
            'other_allowance': 0.0,
            'hra': 1500.0,
            'struct_id': cls.env.ref('om_hr_payroll.structure_base').id,
        })
        cls.employee._compute_current_version_id()

        cls.unpaid_type = cls.env['hr.leave.type'].create({
            'name': 'Test Unpaid Leave (gate)',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'request_unit': 'day',
            'is_unpaid_leave': True,
        })

        cls.month_start = date(2026, 8, 1)
        cls.month_end = date(2026, 8, 31)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _unpaid_leave(self):
        return self.env['hr.leave'].with_context(
            tracking_disable=True, mail_create_nosubscribe=True,
        ).create({
            'employee_id': self.employee.id,
            'holiday_status_id': self.unpaid_type.id,
            'request_date_from': date(2026, 8, 4),
            'request_date_to': date(2026, 8, 20),
        })

    def _payslip(self):
        return self.env['hr.payslip'].create({
            'employee_id': self.employee.id,
            'date_from': self.month_start,
            'date_to': self.month_end,
            'version_id': self.version.id,
            'struct_id': self.version.struct_id.id,
        })

    def _confirm_return(self, leave, day):
        leave.sudo().write({'x_return_date': day})
        leave.with_user(self.manager_user).sudo().\
            action_confirm_return_manager()

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------
    def test_unpaid_leave_is_seen_by_the_gate(self):
        leave = self._unpaid_leave()
        unresolved = self.env['hr.payslip']._get_unresolved_vacation_leaves(
            self.employee.id, self.month_end)
        self.assertIn(leave, unresolved)

    def test_compute_sheet_refuses_while_the_return_is_open(self):
        self._unpaid_leave()
        slip = self._payslip()
        with self.assertRaises(ValidationError) as caught:
            slip.compute_sheet()
        message = str(caught.exception)
        self.assertIn(self.manager_user.name, message,
                      'The error must name who is holding it up.')

    def test_the_batch_skips_the_employee(self):
        self._unpaid_leave()
        reason = self.env['hr.payslip.employees']._check_employee_for_batch(
            self.employee, self.month_start, self.month_end)
        self.assertTrue(reason)
        self.assertIn(self.manager_user.name, reason)

    def test_confirming_the_return_releases_the_payslip(self):
        leave = self._unpaid_leave()
        self._confirm_return(leave, date(2026, 8, 21))

        self.assertFalse(
            self.env['hr.payslip']._get_unresolved_vacation_leaves(
                self.employee.id, self.month_end))
        reason = self.env['hr.payslip.employees']._check_employee_for_batch(
            self.employee, self.month_start, self.month_end)
        self.assertEqual(reason, '')
        self._payslip().compute_sheet()   # must not raise

    def test_an_early_return_is_paid_from_the_return_date(self):
        """The days after the confirmed return leave the unpaid period."""
        leave = self._unpaid_leave()
        self._confirm_return(leave, date(2026, 8, 11))

        self.assertEqual(leave.request_date_to, date(2026, 8, 10))
        slip = self._payslip()
        slip.compute_sheet()
        self.assertTrue(slip.worked_days_line_ids)
