from datetime import datetime
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestItemMovement(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Import = cls.env['ksw.bas.gl.import']
        cls.cc_root = Import._cost_centre_root_plan()
        cls.emp_root = Import._employee_root_plan()
        Account = cls.env['account.analytic.account']
        cls.vehicle = Account.create({
            'name': 'T-TEST-197', 'code': 'ZZ901', 'x_bas_code': 'ZZ901',
            'plan_id': cls.cc_root.id})
        cls.driver_main = Account.create({
            'name': 'TEST DRIVER 1365', 'code': 'ZZ902', 'x_bas_code': 'ZZ902',
            'plan_id': cls.cc_root.id})
        cls.driver_emp = Account.create({
            'name': 'TEST DRIVER 1365', 'code': 'ZZ902', 'x_bas_code': 'ZZ902',
            'plan_id': cls.emp_root.id})

    def _row(self, **kw):
        row = {
            'FTYPE': '600', 'FTYPE2': 3, 'CODE2': '185', 'NUMBER1': 8054521.0,
            'FDATE': datetime(2026, 10, 21, 22, 14), 'ICODE': '11032',
            'IDSCR': ' مياه عذبة تريلات', 'QUAN': 32.0, 'IUNIT': 'cubic m',
            'PRICE': 13.0, 'AMOUNT': 416.0, 'TAX_AMOUNT': 62.4,
            'FCODE': '120301053', 'FNAME': 'Nesma United', 'FACTORE': 1.0,
            'PHRDIS': 1.25, 'cc1': 'T-TEST-197', 'cc2': 'TEST\xa0DRIVER  1365',
            'cc3': 'مستودع تبوك 2',
        }
        row.update(kw)
        return row

    def _show(self, rows, **wizard_vals):
        wizard = self.env['ksw.bas.item.movement.wizard'].create(wizard_vals)
        wref = {'t-test-197': 'ZZ901', 'test driver 1365': 'ZZ902'}
        with patch.object(type(wizard), '_fetch_bas', return_value=(rows, wref)):
            action = wizard.action_show()
        return wizard, action

    def test_line_carries_analytic_accounts_and_both_multipliers(self):
        wizard, action = self._show([self._row()])
        line = wizard.line_ids
        self.assertEqual(len(line), 1)
        self.assertEqual(line.vehicle_account_id, self.vehicle)
        # The driver goes to the Employee Cost Centre plan when it has him,
        # whatever spacing BAS used in the name.
        self.assertEqual(line.driver_account_id, self.driver_emp)
        self.assertEqual(line.customer_factor, 1.0)
        self.assertEqual(line.invoice_factor, 1.25)
        self.assertAlmostEqual(line.factor_difference, 0.25)
        self.assertTrue(line.factors_differ)
        self.assertFalse(line.invoice_factor_missing)
        self.assertEqual(str(line.date), '2026-10-21')
        self.assertEqual(line.document, '185/8054521')
        self.assertEqual(line.warehouse, 'مستودع تبوك 2')
        self.assertAlmostEqual(line.total, 478.4)
        self.assertEqual(action['res_model'], 'ksw.bas.item.movement.line')
        self.assertIn(('wizard_id', '=', wizard.id), action['domain'])
        self.assertEqual(action['context']['group_by'], ['driver_account_id', 'invoice_factor'])

    def test_unknown_cost_centre_kept_as_text(self):
        wizard, _action = self._show([self._row(cc1='NOT IN WREF', cc2='')])
        line = wizard.line_ids
        self.assertFalse(line.vehicle_account_id)
        self.assertEqual(line.vehicle_cost_centre, 'NOT IN WREF')
        self.assertFalse(line.driver_account_id)
        self.assertFalse(line.driver_cost_centre)

    def test_driver_falls_back_to_main_plan(self):
        self.driver_emp.unlink()
        wizard, _action = self._show([self._row()])
        self.assertEqual(wizard.line_ids.driver_account_id, self.driver_main)

    def test_missing_invoice_multiplier_flagged(self):
        wizard, _action = self._show([self._row(PHRDIS=None, FACTORE=None)])
        line = wizard.line_ids
        self.assertTrue(line.invoice_factor_missing)
        self.assertFalse(line.factors_differ)

    def test_only_differences_and_rerun_replaces_lines(self):
        rows = [self._row(), self._row(NUMBER1=1.0, PHRDIS=1.0)]
        wizard, action = self._show(rows, only_differences=True)
        self.assertEqual(len(wizard.line_ids), 2)
        shown = self.env['ksw.bas.item.movement.line'].search(action['domain'])
        self.assertEqual(len(shown), 1)
        with patch.object(type(wizard), '_fetch_bas', return_value=([self._row()], {})):
            wizard.action_show()
        self.assertEqual(len(wizard.line_ids), 1)

    def test_flat_layout_has_no_grouping(self):
        _wizard, action = self._show([self._row()], layout='flat')
        self.assertEqual(action['context']['group_by'], [])

    def test_where_filters_items_and_branches(self):
        branch = self.env['ksw.bas.branch'].create({'code': 'Z99', 'name': 'Test Branch'})
        wizard = self.env['ksw.bas.item.movement.wizard'].create({
            'item_codes': '11032, 11006', 'branch_ids': [(6, 0, branch.ids)]})
        where, params = wizard._where()
        self.assertIn('ICODE', where)
        self.assertIn('CODE2', where)
        self.assertIn('11006', params)
        self.assertIn('Z99', params)
