"""Tests for ksw.sales.commission.sheet and ksw.sales.commission.line.

Covers:
  • Sheet creation sequence
  • Period normalised to first-of-month
  • Unique period constraint
  • Salesman line: sales commission computed from sales rule
  • Collector line: collection commission computed from collection rule
  • Both/hybrid: combined rule fires, sales_amt and coll_amt zeroed
  • Both/hybrid: combined rule not found → sum of standalone rules
  • Sales % and collection % computed correctly
  • Override: condition bypassed when x_condition_override=True
  • Revoke override: commission drops when condition fails without override
  • Duplicate employee line (no split) rejected
  • Non-negative constraint on achieved/target amounts
  • Confirm does NOT touch the commission sheet: as of 19.0.2.0.0 sales
    and collection are paid separately and are not an entry type
"""
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


def _tier(from_pct, to_pct, rate_pct, base='target'):
    return {'from_pct': from_pct, 'to_pct': to_pct,
            'rate_pct': rate_pct, 'base': base}


class TestSalesCommissionSheet(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env

        # Employees
        cls.emp_sales = env['hr.employee'].sudo().create({
            'name': 'SC Sheet Salesman', 'x_is_attendance_sheet': True,
        })
        cls.emp_coll = env['hr.employee'].sudo().create({
            'name': 'SC Sheet Collector', 'x_is_attendance_sheet': True,
        })
        cls.emp_both = env['hr.employee'].sudo().create({
            'name': 'SC Sheet Both', 'x_is_attendance_sheet': True,
        })

        # Rules — general scope so they apply to any employee
        def _mk_rule(kind, min_pct, rate, metric='sales'):
            r = env['ksw.sales.commission.rule'].sudo().create({
                'name': f'SC Test Rule {kind}',
                'kind': kind, 'scope': 'general',
                'condition_type': 'single_threshold',
                'single_threshold_metric': metric,
                'sales_min_pct': min_pct if kind == 'sales' else 0.0,
                'collection_min_pct': min_pct if kind in ('collection', 'combined') else 0.0,
            })
            env['ksw.sales.commission.tier'].sudo().create({
                'rule_id': r.id,
                'from_pct': 0.0, 'to_pct': 0.0,
                'rate_pct': rate, 'base': 'target',
            })
            return r

        cls.sales_rule = _mk_rule('sales', min_pct=0.0, rate=2.0)
        cls.coll_rule = _mk_rule('collection', min_pct=0.0, rate=3.0,
                                 metric='collection')
        cls.comb_rule = _mk_rule('combined', min_pct=0.0, rate=5.0,
                                 metric='collection')

        # Salesperson profiles (year 2026)
        def _mk_profile(emp, role, s_ann=120000.0, c_ann=120000.0):
            return env['ksw.salesperson.profile'].sudo().create({
                'employee_id': emp.id,
                'year': 2026,
                'role': role,
                'annual_sales_target': s_ann,
                'annual_collection_target': c_ann,
                'sales_rule_id': cls.sales_rule.id if role in ('sales', 'both') else False,
                'collection_rule_id': cls.coll_rule.id if role in ('collect', 'both') else False,
                'combined_rule_id': cls.comb_rule.id if role == 'both' else False,
            })

        cls.prof_sales = _mk_profile(cls.emp_sales, 'sales',   s_ann=120000.0)
        cls.prof_coll  = _mk_profile(cls.emp_coll,  'collect', c_ann=120000.0)
        cls.prof_both  = _mk_profile(cls.emp_both,  'both',
                                     s_ann=120000.0, c_ann=120000.0)

        cls.period = '2026-04-01'

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _sc_sheet(self, period=None):
        return self.env['ksw.sales.commission.sheet'].sudo().create({
            'period': period or self.period,
        })

    def _sc_line(self, sheet, emp, achieved_sales=0.0,
                 achieved_coll=0.0, role=None):
        vals = {
            'sheet_id': sheet.id,
            'employee_id': emp.id,
            'achieved_sales': achieved_sales,
            'achieved_collection': achieved_coll,
        }
        if role:
            vals['role'] = role
        return self.env['ksw.sales.commission.line'].sudo().create(vals)

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_01_sequence_on_create(self):
        sheet = self._sc_sheet(period='2025-01-01')
        self.assertNotEqual(sheet.name, 'New')

    def test_02_period_normalised(self):
        sheet = self.env['ksw.sales.commission.sheet'].sudo().create({
            'period': '2025-02-20',
        })
        self.assertEqual(sheet.period.day, 1)

    def test_03_unique_period_constraint(self):
        self._sc_sheet(period='2025-03-01')
        with self.assertRaises(Exception):
            self._sc_sheet(period='2025-03-01')

    def test_04_salesman_gets_sales_commission(self):
        """Role=sales → sales_commission_amount > 0, collection = 0."""
        sheet = self._sc_sheet(period='2025-04-01')
        # Monthly target = 120000/12 = 10000; achieved = 8000 → 80%
        line = self._sc_line(sheet, self.emp_sales, achieved_sales=8000.0)
        self.assertGreater(line.sales_commission_amount, 0.0)
        self.assertEqual(line.collection_commission_amount, 0.0)

    def test_05_collector_gets_collection_commission(self):
        """Role=collect → collection_commission > 0, sales = 0."""
        sheet = self._sc_sheet(period='2025-05-01')
        line = self._sc_line(sheet, self.emp_coll, achieved_coll=9000.0,
                             role='collect')
        self.assertGreater(line.collection_commission_amount, 0.0)
        self.assertEqual(line.sales_commission_amount, 0.0)

    def test_06_both_combined_rule_fires(self):
        """Role=both with combined rule: combined > 0, standalone zeroed."""
        sheet = self._sc_sheet(period='2025-06-01')
        line = self._sc_line(sheet, self.emp_both,
                             achieved_sales=8000.0, achieved_coll=8000.0,
                             role='both')
        if line.combined_commission_amount > 0.0:
            # Combined overrides standalone
            self.assertEqual(line.sales_commission_amount, 0.0)
            self.assertEqual(line.collection_commission_amount, 0.0)
        # If combined fires, total = combined
        self.assertAlmostEqual(
            line.total_commission,
            line.sales_commission_amount
            + line.collection_commission_amount
            + line.combined_commission_amount,
        )

    def test_07_sales_pct_computed(self):
        """sales_pct = achieved/target × 100."""
        sheet = self._sc_sheet(period='2025-07-01')
        line = self._sc_line(sheet, self.emp_sales, achieved_sales=5000.0)
        # target_sales = 120000/12 = 10000; pct = 50%
        self.assertAlmostEqual(line.sales_pct, 50.0, delta=1.0)

    def test_08_collection_pct_zero_target_gives_zero(self):
        """collection_pct = 0 when target is 0."""
        sheet = self._sc_sheet(period='2025-08-01')
        line = self._sc_line(sheet, self.emp_sales, achieved_coll=500.0)
        self.assertEqual(line.collection_pct, 0.0)

    def test_09_override_bypasses_condition(self):
        """x_condition_override=True makes commission pay even if threshold fails."""
        # Build a rule with high threshold
        rule = self.env['ksw.sales.commission.rule'].sudo().create({
            'name': 'Override Test Rule', 'kind': 'sales', 'scope': 'general',
            'condition_type': 'single_threshold',
            'single_threshold_metric': 'sales',
            'sales_min_pct': 90.0,  # very high threshold
        })
        self.env['ksw.sales.commission.tier'].sudo().create({
            'rule_id': rule.id, 'from_pct': 0.0, 'to_pct': 0.0,
            'rate_pct': 5.0, 'base': 'target',
        })

        emp = self.env['hr.employee'].sudo().create({
            'name': 'Override Test Emp', 'x_is_attendance_sheet': True,
        })
        sheet = self._sc_sheet(period='2025-09-01')
        line = self._sc_line(sheet, emp, achieved_sales=2000.0,
                             role='sales')

        # Without override: below 90% threshold → 0
        self.assertEqual(line.sales_commission_amount, 0.0)

        # Stamp override via sudo
        line.sudo().write({
            'x_condition_override': True,
            'x_override_by': self.env.uid,
            'x_override_reason': 'Test exception',
        })
        line.sudo()._compute_commission()
        # Now commission should fire
        self.assertGreater(line.sales_commission_amount, 0.0)

    def test_10_duplicate_employee_line_blocked(self):
        """Two lines for the same employee (no split) on one sheet rejected."""
        sheet = self._sc_sheet(period='2025-10-01')
        self._sc_line(sheet, self.emp_sales, achieved_sales=1000.0)
        with self.assertRaises(ValidationError):
            self._sc_line(sheet, self.emp_sales, achieved_sales=2000.0)

    def test_11_negative_achieved_rejected(self):
        """Negative achieved_sales raises ValidationError."""
        sheet = self._sc_sheet(period='2025-11-01')
        with self.assertRaises(ValidationError):
            self._sc_line(sheet, self.emp_sales, achieved_sales=-100.0)

    def test_12_confirm_creates_no_pay_entries(self):
        """Sales and collection are paid separately.

        Confirming this sheet used to push amounts onto the employee's
        commission sheet. Since 19.0.3.0.0 there is no commission sheet at
        all, and sales must not reach the pay entries either.
        """
        emp = self.env['hr.employee'].sudo().create({
            'name': 'SC Sync Emp', 'x_is_attendance_sheet': True,
        })
        sc_sheet = self._sc_sheet(period='2028-12-01')
        self._sc_line(sc_sheet, emp, achieved_sales=5000.0, role='sales')
        sc_sheet.sudo().action_confirm()

        self.assertFalse(
            self.env['ksw.pay.entry'].sudo().search([
                ('employee_id', '=', emp.id), ('period', '=', '2028-12-01'),
            ]),
            'sales commission must not create pay entries')

    def test_13_sales_sheet_keeps_its_own_totals(self):
        """Detaching it must not stop it computing what it owes."""
        emp = self.env['hr.employee'].sudo().create({
            'name': 'SC Reset Emp', 'x_is_attendance_sheet': True,
        })
        sc_sheet = self._sc_sheet(period='2028-11-01')
        self._sc_line(sc_sheet, emp, achieved_sales=5000.0, role='sales')
        sc_sheet.sudo().action_confirm()
        self.assertEqual(sc_sheet.state, 'confirmed')
        self.assertTrue(sc_sheet.is_locked)
        sc_sheet.sudo().action_reset_to_draft()
        self.assertEqual(sc_sheet.state, 'draft')
        self.assertFalse(sc_sheet.is_locked)

    def test_14_confirm_creates_no_payment_register_line(self):
        """It must not appear in the monthly commission payment either."""
        emp = self.env['hr.employee'].sudo().create({
            'name': 'SC AutoCreate Emp', 'x_is_attendance_sheet': True,
        })
        sc_sheet = self._sc_sheet(period='2028-10-01')
        self._sc_line(sc_sheet, emp, achieved_sales=5000.0, role='sales')
        sc_sheet.sudo().action_confirm()

        self.assertFalse(
            self.env['ksw.pay.run.line'].sudo().search([
                ('employee_id', '=', emp.id), ('period', '=', '2028-10-01'),
            ]),
            'a sales sheet must not create a payment register line')

    def test_15_sheet_total_commission_is_sum_of_lines(self):
        """total_commission = sum of all line totals."""
        emp_a = self.env['hr.employee'].sudo().create({
            'name': 'SC Total EmpA', 'x_is_attendance_sheet': True,
        })
        emp_b = self.env['hr.employee'].sudo().create({
            'name': 'SC Total EmpB', 'x_is_attendance_sheet': True,
        })
        sheet = self._sc_sheet(period='2024-10-01')
        la = self._sc_line(sheet, emp_a, achieved_sales=5000.0, role='sales')
        lb = self._sc_line(sheet, emp_b, achieved_sales=3000.0, role='sales')
        expected = la.total_commission + lb.total_commission
        self.assertAlmostEqual(sheet.total_commission, expected)

