"""Tests for ksw.driver.commission.sheet and ksw.driver.commission.line."""
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase
class TestDriverCommission(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.site = env['ksw.site'].sudo().create({
            'name': 'Test Site Alpha', 'code': 'TSA',
            'required_trips_full_month': 50,
            'tier2_trips': 40, 'tier2_rate': 10.0,
            'tier3_trips': 40, 'tier3_rate': 15.0,
            'tier4_trips': 40, 'tier4_rate': 20.0,
            'tier5_rate': 25.0,
        })
        dept = env['hr.department'].create({'name': 'Driver Dept'})
        cls.emp1 = env['hr.employee'].sudo().create({
            'name': 'Driver One', 'department_id': dept.id,
            'x_is_attendance_sheet': True, 'x_site_id': cls.site.id,
        })
        cls.period = '2026-04-01'
    def _ds(self, **kw):
        v = dict(site_id=self.site.id, period=self.period)
        v.update(kw)
        return self.env['ksw.driver.commission.sheet'].sudo().create(v)
    def _line(self, sheet, emp, worked, trips, multiplied=None):
        """Create a driver commission line.

        ``multiplied`` sets ``multiplied_trips`` directly.
        When omitted it defaults to ``trips`` (actual = multiplied, no factor).
        """
        return self.env['ksw.driver.commission.line'].sudo().create({
            'sheet_id': sheet.id, 'employee_id': emp.id,
            'worked_days': worked, 'actual_trips': trips,
            'multiplied_trips': multiplied if multiplied is not None else trips,
        })
    def test_01_required_trips_full_month(self):
        ds = self._ds(); l = self._line(ds, self.emp1, 30, 0)
        self.assertEqual(l.required_trips, 50)
    def test_02_partial_month_pro_rata(self):
        ds = self._ds()
        l = self._line(ds, self.emp1, 19, 0)
        self.assertEqual(l.required_trips, round(50*19/30))
    def test_03_zero_commission_below_required(self):
        ds = self._ds(); l = self._line(ds, self.emp1, 30, 45)
        self.assertEqual(l.total_commission, 0.0)
    def test_04_tier2_only(self):
        ds = self._ds(); l = self._line(ds, self.emp1, 30, 60)
        self.assertEqual(l.tier2_trips, 10)
        self.assertAlmostEqual(l.total_commission, 100.0)
    def test_05_tiers_2_and_3(self):
        ds = self._ds(); l = self._line(ds, self.emp1, 30, 130)
        self.assertEqual(l.tier2_trips, 40); self.assertEqual(l.tier3_trips, 40)
        self.assertAlmostEqual(l.total_commission, 40*10 + 40*15)
    def test_06_all_five_tiers(self):
        ds = self._ds(); l = self._line(ds, self.emp1, 30, 190)
        self.assertEqual(l.tier5_trips, 20)
        self.assertAlmostEqual(l.total_commission, 40*10+40*15+40*20+20*25)
    def test_07_multiplier_applied(self):
        """Setting multiplied_trips independently of actual_trips works."""
        ds = self._ds()
        l = self._line(ds, self.emp1, 30, 40, multiplied=60)
        self.assertEqual(l.multiplied_trips, 60)
        # required=50, above=max(60-50,0)=10, tier2 rate=10 → 1 tier-2 trip × 10 = 100
        self.assertAlmostEqual(l.total_commission, 100.0)
    def test_08_confirm_sets_state(self):
        ds = self._ds(); self._line(ds, self.emp1, 30, 60)
        ds.action_confirm()
        self.assertEqual(ds.state, 'confirmed')
    def test_09_unique_site_period(self):
        self._ds()
        with self.assertRaises(Exception):
            self._ds()
    def test_10_sheet_total_is_sum_of_lines(self):
        ds = self._ds()
        emp2 = self.env['hr.employee'].sudo().create({
            'name': 'Driver Two', 'x_is_attendance_sheet': True,
        })
        self._line(ds, self.emp1, 30, 60)  # 100 SAR
        self._line(ds, emp2, 30, 60)       # 100 SAR
        self.assertAlmostEqual(ds.total_commission, 200.0)


class _FakeCursor:
    """Minimal pymssql-cursor stand-in that records calls and returns rows."""
    def __init__(self, rows, calls):
        self._rows = rows
        self._calls = calls

    def execute(self, sql, params=None):
        self._calls.append((sql, params))

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    def __init__(self, rows, calls):
        self._rows = rows
        self._calls = calls
        self.closed = False

    def cursor(self, as_dict=False):
        return _FakeCursor(self._rows, self._calls)

    def close(self):
        self.closed = True


class TestDriverCommissionBasPull(TransactionCase):
    """action_pull_from_bas: BAS trip filling, roster, and flagging."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.site = env['ksw.site'].sudo().create({
            'name': 'Tabuk 2', 'code': 'TBK2',
            'required_trips_full_month': 50,
            'tier2_trips': 40, 'tier2_rate': 10.0,
            'tier3_trips': 40, 'tier3_rate': 15.0,
            'tier4_trips': 40, 'tier4_rate': 20.0,
            'tier5_rate': 25.0,
        })
        cls.other_site = env['ksw.site'].sudo().create({'name': 'Other'})
        # Mapped driver with BAS data
        cls.emp_mapped = env['hr.employee'].sudo().create({
            'name': 'Wahab (Odoo)', 'x_is_attendance_sheet': True,
            'x_site_id': cls.site.id,
            'x_bas_driver_cost_center': 'WAHAB JAN1387',
        })
        # Mapped driver but BAS returns nothing for it
        cls.emp_nodata = env['hr.employee'].sudo().create({
            'name': 'Ghost Driver', 'x_is_attendance_sheet': True,
            'x_site_id': cls.site.id,
            'x_bas_driver_cost_center': 'NOBODY 000',
        })
        # Roster driver with no BAS cost center set
        cls.emp_unmapped = env['hr.employee'].sudo().create({
            'name': 'Unmapped Driver', 'x_is_attendance_sheet': True,
            'x_site_id': cls.site.id,
        })
        # Driver on a different site — must be ignored
        cls.emp_other = env['hr.employee'].sudo().create({
            'name': 'Other Site Driver', 'x_is_attendance_sheet': True,
            'x_site_id': cls.other_site.id,
            'x_bas_driver_cost_center': 'SOMEONE 111',
        })
        cls.period = '2026-07-01'
        # BAS returns real-shaped rows only for the mapped driver.
        cls.bas_rows = [
            {'driver_cc': 'WAHAB JAN1387', 'loads': 2, 'mult': 4.0,
             'equip': 'T164'},
        ]

    def _sheet(self):
        return self.env['ksw.driver.commission.sheet'].sudo().create({
            'site_id': self.site.id, 'period': self.period,
        })

    def _pull(self, sheet):
        calls = []
        conn = _FakeConn(self.bas_rows, calls)
        connector = type(self.env['ksw.bas.connector'])
        with patch.object(connector, '_bas_connect', return_value=conn):
            res = sheet.action_pull_from_bas()
        self.assertTrue(conn.closed, "BAS connection must be closed")
        return res, calls

    def test_pull_fills_and_rosters(self):
        ds = self._sheet()
        res, calls = self._pull(ds)
        # One row per site roster member (3), other-site driver excluded.
        self.assertEqual(len(ds.line_ids), 3)
        by_emp = {l.employee_id: l for l in ds.line_ids}
        self.assertNotIn(self.emp_other, by_emp)

        mapped = by_emp[self.emp_mapped]
        self.assertEqual(mapped.actual_trips, 2)
        self.assertAlmostEqual(mapped.multiplied_trips, 4.0)

        self.assertEqual(by_emp[self.emp_nodata].actual_trips, 0)
        self.assertEqual(by_emp[self.emp_unmapped].actual_trips, 0)

        # Query used the configured item code + ftype (drivers matched in Python).
        self.assertTrue(calls)
        params = calls[0][1]
        self.assertIn('11032', params)           # default item code
        self.assertIn('600', params)             # default ftype

    def test_pull_matches_despite_nbsp_and_case(self):
        """BAS non-breaking spaces / casing must not break driver matching."""
        # BAS returns the driver with NBSP separators and different casing.
        self.bas_rows = [
            {'driver_cc': 'wahab\xa0jan1387', 'loads': 3, 'mult': 4.5,
             'equip': 'T164'},
        ]
        ds = self._sheet()
        self._pull(ds)
        line = ds.line_ids.filtered(lambda l: l.employee_id == self.emp_mapped)
        self.assertEqual(line.actual_trips, 3)
        self.assertAlmostEqual(line.multiplied_trips, 4.5)

    def test_pull_multiplied_is_fractional(self):
        """الرد المضاعف is stored as a fractional value, not rounded."""
        self.bas_rows = [
            {'driver_cc': 'WAHAB JAN1387', 'loads': 5, 'mult': 7.75,
             'equip': 'T164'},
        ]
        ds = self._sheet()
        self._pull(ds)
        line = ds.line_ids.filtered(lambda l: l.employee_id == self.emp_mapped)
        self.assertAlmostEqual(line.multiplied_trips, 7.75)

    def test_pull_is_idempotent(self):
        ds = self._sheet()
        self._pull(ds)
        self._pull(ds)
        self.assertEqual(len(ds.line_ids), 3)  # updated, not duplicated

    def test_pull_only_on_draft(self):
        ds = self._sheet()
        self._pull(ds)          # creates lines so confirm is meaningful
        ds.action_confirm()
        with self.assertRaises(UserError):
            self._pull(ds)
