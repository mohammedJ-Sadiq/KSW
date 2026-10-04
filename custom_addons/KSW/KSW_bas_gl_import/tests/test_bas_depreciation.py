from datetime import date

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBasDepreciation(TransactionCase):
    """BAS's depreciation rule, pinned to figures read off BAS's own report."""

    def _asset(self, cost, accum, events=()):
        acc = self.env['account.account'].search([], limit=1)
        return self.env['ksw.bas.fixed.asset'].create({
            'code': 'T', 'year': 2026, 'date': date(2026, 1, 1),
            'asset_account_id': acc.id, 'expense_account_id': acc.id,
            'expense_group': '3304', 'rate': 15.0, 'cost': cost, 'accum_open': accum,
            'company_id': self.env.company.id,
            'event_ids': [(0, 0, e) for e in events],
        })

    def test_addition_before_range_is_merged(self):
        """1904010018, BAS report 1 Jul - 31 Aug 2026: an addition of
        10,358.70 on 17 May reopens the written-off cost for the next range."""
        asset = self._asset(1201996.53, 1192502.78, [
            {'date': date(2026, 5, 17), 'kind': 'add', 'amount': 10358.70}])
        self.assertAlmostEqual(asset._bas_charge(date(2026, 1, 1), date(2026, 6, 30)), 9684.32, delta=0.01)
        self.assertAlmostEqual(asset._bas_charge(date(2026, 7, 1), date(2026, 8, 31)), 10167.13, delta=0.01)

    def test_addition_inside_range_runs_from_its_date(self):
        """1904010011, same report: 4,996 base + 2,660.44 added 19 Aug = 141.51."""
        asset = self._asset(4996.0, 2351.88 - 4996.0 * 0.15 / 365 * 181, [
            {'date': date(2026, 8, 19), 'kind': 'add', 'amount': 2660.44}])
        self.assertAlmostEqual(asset._bas_charge(date(2026, 7, 1), date(2026, 8, 31)), 141.51, delta=0.01)

    def test_stops_at_one_riyal(self):
        asset = self._asset(19647.72, 19646.72)          # 1904010012
        self.assertEqual(asset._bas_charge(date(2026, 1, 1), date(2026, 12, 31)), 0.0)

    def test_disposal_charges_recorded_amount(self):
        """1906010073: disposed 30 Apr with DEP_AMOUNT = opening accumulated."""
        asset = self._asset(109176.0, 43834.91, [
            {'date': date(2026, 4, 30), 'kind': 'dispose', 'amount': 109176.0, 'dep_amount': 43834.91}])
        self.assertEqual(asset._bas_charge(date(2026, 1, 1), date(2026, 6, 30)), 0.0)

    def test_disposal_mid_year_range_is_absolute(self):
        """1906010073 for Q2 2026: BAS's statement charges 4,038.02 =
        |43,834.91 recorded at disposal - 47,872.93 accumulated at 1 Apr|."""
        asset = self._asset(109176.0, 43834.91, [
            {'date': date(2026, 4, 30), 'kind': 'dispose', 'amount': 109176.0, 'dep_amount': 43834.91}])
        self.assertAlmostEqual(asset._bas_charge(date(2026, 4, 1), date(2026, 6, 30)), 4038.02, delta=0.01)
