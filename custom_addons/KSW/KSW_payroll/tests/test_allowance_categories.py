# -*- coding: utf-8 -*-
"""An allowance must reach GROSS — a category with no parent never does.

`GROSS = categories.BASIC + categories.ALW`, and
`_sum_salary_rule_category` (om_hr_payroll) accumulates an amount into its
category *and* every parent above it.  So an allowance whose category has
no parent is added to nothing: the line prints on the payslip, and GROSS —
and therefore NET — ignore it.  The employee is simply not paid for it.

Upstream ships `HRA`, `Other` and `Travel` parentless.  HRA and Other were
parented by hand in the production UI; Travel never was, which is why a
Transportation Allowance was displayed but never paid.
"""
from datetime import date

from odoo.tests.common import TransactionCase


class TestAllowanceCategories(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.employee = cls.env['hr.employee'].create({'name': 'Travel Allowance Employee'})
        cls.version = cls.employee.current_version_id
        cls.version.write({
            'name': 'Travel Test Version',
            'date_version': date(2025, 1, 1),
            'contract_date_start': date(2025, 1, 1),
            'wage': 3000.0,
            'hra': 0.0,
            'da': 0.0,
            'travel_allowance': 500.0,
            'meal_allowance': 0.0,
            'medical_allowance': 0.0,
            'other_allowance': 0.0,
            'struct_id': cls.env.ref('om_hr_payroll.structure_base').id,
        })
        cls.employee._compute_current_version_id()

    def _category(self, xmlid):
        return self.env.ref('om_hr_payroll.%s' % xmlid)

    def test_every_allowance_category_rolls_up_into_alw(self):
        """Parentless is the bug — assert the parenting itself."""
        alw = self._category('ALW')
        for code in ('HRA', 'Other', 'Travel'):
            self.assertEqual(
                self._category(code).parent_id, alw,
                '%s must roll up into ALW or its amount never reaches GROSS.'
                % code)

    def test_a_travel_allowance_is_inside_gross(self):
        """GROSS must be BASIC plus every line that rolls up into ALW.

        The fixture also picks up a Saturday short-shift overtime credit
        (SAT_OT, paid through the `Other` rule), so the figure is asserted
        as the invariant rather than a hard-coded total — the point is
        that Travel is one of the contributions, not that it is the only
        one.
        """
        slip = self.env['hr.payslip'].create({
            'employee_id': self.employee.id,
            'name': 'Travel Test',
            'date_from': date(2026, 4, 1),
            'date_to': date(2026, 4, 30),
            'struct_id': self.env.ref('om_hr_payroll.structure_base').id,
            'version_id': self.version.id,
        })
        slip.compute_sheet()

        def total(code):
            line = slip.line_ids.filtered(lambda l: l.code == code)[:1]
            return line.total if line else 0.0

        def rolls_into_alw(category):
            while category:
                if category.code == 'ALW':
                    return True
                category = category.parent_id
            return False

        self.assertEqual(total('Travel'), 500.0, 'Fixture: the line is produced.')
        self.assertEqual(total('BASIC'), 3000.0)

        contributions = slip.line_ids.filtered(
            lambda l: rolls_into_alw(l.category_id))
        self.assertIn('Travel', contributions.mapped('code'),
                      'Travel must roll up into ALW.')
        self.assertEqual(
            total('GROSS'),
            total('BASIC') + sum(contributions.mapped('total')),
            'GROSS must be BASIC plus everything under ALW.')
        self.assertEqual(
            total('GROSS') - total('Other') - total('BASIC'), 500.0,
            'The transportation allowance is inside GROSS.')
