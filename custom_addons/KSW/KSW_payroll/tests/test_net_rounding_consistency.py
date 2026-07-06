# -*- coding: utf-8 -*-
"""Tests for NET salary rounding consistency.

Root cause (fixed June 2026):
  The KSW hr.payslip.line override sets amount = fields.Float(digits=(16, 0)),
  storing all amounts as integers.  The base engine accumulates category sums
  using currency.round (SAR = 2 dp), so a fractional input such as 87.5 SAR
  enters categories.DED as -87.5, giving NET = GROSS - 609 - 87.5 = 6153.5,
  which rounds to 6154 (banker's rounding) when stored.  But the displayed
  KSW_DEDUCTIONS line shows -88 (87.5 → integer), so the user sees
  6850 - 609 - 88 = 6153 ≠ NET 6154.

The fix in compute_sheet() re-derives NET from the already-rounded stored
amounts: NET = GROSS.amount + Σ(DED line amounts), eliminating the gap.

Scenarios covered:
  A: All integer inputs            → NET consistent, no change needed.
  B: Single fractional KSW_DED    → NET adjusted to match rounded sum.
  C: Two fractional KSW_DED lines → NET adjusted to match rounded sum.
  D: Fractional that rounds DOWN   → consistent even when rounding goes the
                                     other way (e.g. 87.4 → 87, not 88).

In every case the invariant is:
    NET line amount  ==  GROSS line amount  +  Σ(DED category line amounts)
"""
from datetime import date

from odoo.tests.common import TransactionCase


class TestNetRoundingConsistency(TransactionCase):
    """NET salary line must equal GROSS + Σ(DED) using the displayed (rounded)
    integer amounts, regardless of fractional inputs."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # Minimal calendar (no attendance needed for these tests)
        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Net Rounding Test Calendar',
            'tz': 'Asia/Riyadh',
        })

        cls.employee = cls.env['hr.employee'].create({
            'name': 'Net Rounding Test Employee',
            'resource_calendar_id': cls.calendar.id,
        })

        cls.version = cls.employee.current_version_id
        cls.version.write({
            'name': 'Net Rounding Test Version',
            'date_version': date(2025, 1, 1),
            'contract_date_start': date(2025, 1, 1),
            'resource_calendar_id': cls.calendar.id,
            'wage': 5250.0,
            'hra': 1000.0,
            'other_allowance': 600.0,
            'travel_allowance': 0.0,
            'mobile_allowance': 0.0,
            'da': 0.0,
            'meal_allowance': 0.0,
            'medical_allowance': 0.0,
            'struct_id': cls.env.ref('om_hr_payroll.structure_base').id,
        })
        cls.employee._compute_current_version_id()

        cls.month_start = date(2026, 7, 1)
        cls.month_end = date(2026, 7, 31)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_payslip(self):
        """Return a fresh draft payslip for the test employee/month."""
        return self.env['hr.payslip'].create({
            'employee_id': self.employee.id,
            'name': 'Net Rounding Test Slip',
            'date_from': self.month_start,
            'date_to': self.month_end,
            'struct_id': self.env.ref('om_hr_payroll.structure_base').id,
            'version_id': self.version.id,
        })

    def _add_ksw_ded(self, slip, amount, label='Test Deduction'):
        """Attach a KSW_DED_* input line to the payslip (mimics a loan
        installment injected by KSW_deduction's compute_sheet hook)."""
        self.env['hr.payslip.input'].create({
            'name': label,
            'payslip_id': slip.id,
            'code': 'KSW_DED_9999',
            'amount': amount,
            'version_id': self.version.id,
        })

    def _check_consistency(self, slip, msg=''):
        """Assert NET == GROSS + Σ(DED) using the stored integer amounts."""
        lines = {l.code: l for l in slip.line_ids}
        gross_amt = lines['GROSS'].amount
        net_amt = lines['NET'].amount

        ded_sum = sum(
            l.amount for l in slip.line_ids
            if l.category_id.code == 'DED'
        )
        expected_net = gross_amt + ded_sum
        self.assertEqual(
            net_amt, expected_net,
            f'{msg} NET={net_amt} but GROSS({gross_amt}) + DED({ded_sum}) = {expected_net}',
        )
        # Totals (computed) must follow from amounts (digits=(16,0))
        for code in ('GROSS', 'NET'):
            line = lines[code]
            self.assertEqual(
                line.total, line.amount,
                f'{msg} {code}.total({line.total}) != {code}.amount({line.amount})',
            )

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_a_all_integer_inputs_stay_consistent(self):
        """Integer-only inputs: NET = GROSS + DED with no rounding artifact."""
        slip = self._make_payslip()
        self._add_ksw_ded(slip, 88.0, 'Whole-SAR installment')
        slip.compute_sheet()

        # GROSS = wage(5250) + HRA(1000) + Other(600) = 6850
        lines = {l.code: l for l in slip.line_ids}
        self.assertEqual(lines['GROSS'].amount, 6850.0)
        # KSW_DEDUCTIONS = -88
        ksw_line = slip.line_ids.filtered(lambda l: l.code == 'KSW_DEDUCTIONS')
        self.assertEqual(ksw_line[0].amount, -88.0)

        self._check_consistency(slip, 'integer inputs')

    def test_b_half_sar_fractional_input_consistent(self):
        """87.5 SAR installment (the bug case): NET must equal GROSS + DED.

        Without the fix:
            categories.DED gets -87.5 → NET = 6153.5 → stored as 6154
            KSW_DEDUCTIONS stored as -88 (digits=(16,0))
            User sees 6850 - 88 - 0 = 6762 if no GOSI, or similar 1-SAR gap.
        With the fix:
            NET is recomputed from stored amounts → NET = GROSS + (-88) = 6762
            (and similarly for any deductions present).
        """
        slip = self._make_payslip()
        self._add_ksw_ded(slip, 87.5, 'Half-SAR installment')
        slip.compute_sheet()

        lines = {l.code: l for l in slip.line_ids}
        # KSW_DEDUCTIONS must be stored as a whole integer (-88, not -87.5)
        ksw_line = slip.line_ids.filtered(lambda l: l.code == 'KSW_DEDUCTIONS')
        self.assertEqual(ksw_line[0].amount, -88.0,
                         'KSW_DEDUCTIONS should round 87.5 → 88 per digits=(16,0)')

        self._check_consistency(slip, '87.5 fractional input')

    def test_c_two_fractional_inputs_consistent(self):
        """Two fractional installments: NET consistent with rounded DED sum."""
        slip = self._make_payslip()
        # 87.5 + 62.5 = 150 total; individually round to 88 + 63 = 151
        self.env['hr.payslip.input'].create({
            'name': 'Installment A',
            'payslip_id': slip.id,
            'code': 'KSW_DED_1001',
            'amount': 87.5,
            'version_id': self.version.id,
        })
        self.env['hr.payslip.input'].create({
            'name': 'Installment B',
            'payslip_id': slip.id,
            'code': 'KSW_DED_1002',
            'amount': 62.5,
            'version_id': self.version.id,
        })
        slip.compute_sheet()

        # Both inputs sum into a single KSW_DEDUCTIONS line (total = -(87.5+62.5) = -150
        # → stored as -150 via digits=(16,0); no rounding issue here since -150 is exact)
        ksw_line = slip.line_ids.filtered(lambda l: l.code == 'KSW_DEDUCTIONS')
        self.assertEqual(ksw_line[0].amount, -150.0,
                         'KSW_DEDUCTIONS sums both inputs: -(87.5+62.5) = -150')

        self._check_consistency(slip, 'two fractional inputs')

    def test_d_rounds_down_fractional_consistent(self):
        """87.4 SAR rounds DOWN to 87 (not 88); NET is still consistent."""
        slip = self._make_payslip()
        self._add_ksw_ded(slip, 87.4, 'Round-down installment')
        slip.compute_sheet()

        ksw_line = slip.line_ids.filtered(lambda l: l.code == 'KSW_DEDUCTIONS')
        self.assertEqual(ksw_line[0].amount, -87.0,
                         'KSW_DEDUCTIONS should round 87.4 → 87 per digits=(16,0)')

        self._check_consistency(slip, '87.4 round-down fractional input')

    def test_e_no_ksw_ded_inputs_still_consistent(self):
        """With no KSW_DED inputs, NET must still equal GROSS + Σ(DED).

        Note: ATTDED (attendance deduction) may fire even without KSW_DED
        inputs if the employee has no attendance records for the period.
        The invariant we guarantee is consistency, not NET == GROSS.
        """
        slip = self._make_payslip()
        slip.compute_sheet()
        self._check_consistency(slip, 'no KSW_DED inputs')
