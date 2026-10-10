# -*- coding: utf-8 -*-
"""A payslip must read the contract version in effect for its period.

om_hr_payroll's `get_versions` returns every version overlapping the
period and every caller takes ``ids[0]`` — the OLDEST, since `hr.version`
sorts by `date_version`.  KSWCO payslip 19457 (Sep 2026): Other Allowance
1,000 SAR added as a new version from 10 Sep, payslip computed on the
2022 version and paid no allowance.
"""
from datetime import date

from odoo.tests.common import TransactionCase


class TestPayslipEffectiveVersion(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.struct = cls.env.ref('om_hr_payroll.structure_base')
        cls.employee = cls.env['hr.employee'].create({'name': 'Version Pick Employee'})
        cls.old = cls.employee.current_version_id
        cls.old.write({
            'date_version': date(2022, 4, 4),
            'contract_date_start': date(2022, 4, 4),
            'contract_date_end': date(2026, 9, 9),
            'wage': 2500.0, 'other_allowance': 0.0, 'struct_id': cls.struct.id,
        })
        cls.new = cls.old.copy({
            'date_version': date(2026, 9, 10),
            'contract_date_start': date(2026, 9, 10),
            'contract_date_end': False,
            'other_allowance': 1000.0,
        })

    def _pick(self, date_from, date_to):
        return self.env['hr.payslip'].get_versions(self.employee, date_from, date_to)

    def test_mid_month_version_wins(self):
        ids = self._pick(date(2026, 9, 1), date(2026, 9, 30))
        self.assertEqual(ids[0], self.new.id)
        self.assertIn(self.old.id, ids)

    def test_earlier_month_keeps_old_version(self):
        self.assertEqual(self._pick(date(2026, 8, 1), date(2026, 8, 31)), [self.old.id])

    def test_future_dated_version_does_not_win(self):
        """A raise dated after the period is not in effect yet."""
        emp = self.env['hr.employee'].create({'name': 'Future Raise Employee'})
        current = emp.current_version_id
        current.write({
            'date_version': date(2022, 4, 4),
            'contract_date_start': date(2022, 4, 4),
            'wage': 2500.0, 'struct_id': self.struct.id,
        })
        future = current.copy({'date_version': date(2026, 11, 1), 'wage': 3000.0})
        ids = self.env['hr.payslip'].get_versions(emp, date(2026, 9, 1), date(2026, 9, 30))
        self.assertEqual(ids[0], current.id)
        self.assertIn(future.id, ids)

    def test_batch_onchange_picks_effective_version(self):
        res = self.env['hr.payslip'].onchange_employee_id(
            date(2026, 9, 1), date(2026, 9, 30), self.employee.id)
        self.assertEqual(res['value']['version_id'], self.new.id)
