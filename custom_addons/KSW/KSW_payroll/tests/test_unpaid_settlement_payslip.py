# -*- coding: utf-8 -*-
"""The unpaid-leave settlement payslip.

An unpaid leave now settles like an annual vacation at GM final approval:
the accountant's commission lines are paid and the other-deduction lines are
taken on a leave-linked payslip.  What it must NOT do is borrow the annual
parts of the builder — there is no balance to pay out, and the HRA / GOSI
advances cover only the payslip's own month, never the unpaid months.
"""
from datetime import date

from odoo.tests.common import TransactionCase


class TestUnpaidSettlementPayslip(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Unpaid Settlement Test Calendar',
            'tz': 'Asia/Riyadh',
        })
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Unpaid Settlement Test Employee',
            'resource_calendar_id': cls.calendar.id,
            'tz': 'Asia/Riyadh',
            'country_id': cls.env.ref('base.sa').id,
        })
        cls.version = cls.employee.current_version_id
        cls.version.write({
            'name': 'Unpaid Settlement Test Version',
            'date_version': date(2025, 1, 1),
            'contract_date_start': date(2025, 1, 1),
            'resource_calendar_id': cls.calendar.id,
            'wage': 6000.0,
            'hra': 1500.0,
            'struct_id': cls.env.ref('om_hr_payroll.structure_base').id,
        })
        cls.employee._compute_current_version_id()

        cls.unpaid_type = cls.env['hr.leave.type'].create({
            'name': 'Test Unpaid Leave (settlement)',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'request_unit': 'day',
            'is_unpaid_leave': True,
        })

    def _leave_and_payslip(self):
        # Spans Aug → Oct so a multi-month advance would be visible.
        leave = self.env['hr.leave'].with_context(
            tracking_disable=True, mail_create_nosubscribe=True,
        ).create({
            'employee_id': self.employee.id,
            'holiday_status_id': self.unpaid_type.id,
            'request_date_from': date(2026, 8, 17),
            'request_date_to': date(2026, 10, 15),
        })
        leave.sudo().write({
            'x_commission_line_ids': [
                (0, 0, {'name': 'Jul 2026', 'amount': 700.0}),
                (0, 0, {'name': 'Aug 2026', 'amount': 300.0}),
            ],
            'x_deduction_line_ids': [
                (0, 0, {'name': 'Traffic fine', 'amount': 250.0}),
            ],
        })
        payslip = self.env['hr.payslip'].create({
            'employee_id': self.employee.id,
            'date_from': date(2026, 8, 1),
            'date_to': date(2026, 8, 31),
            'version_id': self.version.id,
            'struct_id': self.version.struct_id.id,
            'x_leave_id': leave.id,
        })
        return leave, payslip

    def _inputs(self):
        leave, payslip = self._leave_and_payslip()
        vals = self.env['hr.leave']._build_vacation_input_lines(
            leave, self.employee, payslip)
        return {v['code']: v for v in vals}

    def test_accountant_lines_are_carried(self):
        inputs = self._inputs()
        self.assertEqual(inputs['ADDITIONAL_COMMISSIONS']['amount'], 1000.0)
        self.assertEqual(inputs['OTHER_DEDUCTIONS']['amount'], 250.0)

    def test_no_vacation_balance_is_paid(self):
        self.assertNotIn('VACATION_BAL', self._inputs())

    def test_advances_cover_only_the_settlement_month(self):
        inputs = self._inputs()
        self.assertEqual(inputs['VACATION_HRA']['amount'], 1500.0)
        gosi_month = round((6000.0 + 1500.0) * 9.75 / 100.0)
        self.assertEqual(inputs['VACATION_GOSI']['amount'], gosi_month)
