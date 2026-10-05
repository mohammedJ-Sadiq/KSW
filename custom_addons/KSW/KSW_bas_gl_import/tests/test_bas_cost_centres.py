from unittest.mock import MagicMock, patch

from odoo.tests import TransactionCase, tagged


def _wref(*rows):
    return [{'code': c, 'parent': p, 'NAME': n} for c, p, n in rows]


class _Isolated(TransactionCase):
    """Detach whatever cost centres the database already holds (rolled back
    with the test), so each test builds its own tree from the fake WREF10."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env['account.analytic.account'].with_context(active_test=False).search(
            [('x_bas_code', '!=', False)]).write({'x_bas_code': False})
        cls.env['account.analytic.plan'].search(
            [('x_bas_code', '!=', False)]).write({'x_bas_code': False})


@tagged('post_install', '-at_install')
class TestBasCostCentres(_Isolated):

    TREE = _wref(
        ('1', '', 'مراكز تكلفة شركة الكوثر '),
        ('2', ' ', 'مصنع مياه الحياة'),
        ('14', '1', 'السيارات'),
        ('141', '14', 'التريلات'),
        ('1410002', '141', 'T153'),
        ('1410003', '141', 'T154'),
        ('11', '1', 'الادارة العامة'),
        ('21', '2', 'مركز تكلفة الرئيسي الحياة'),
    )

    def _run(self, rows):
        cursor = MagicMock()
        cursor.fetchall.return_value = rows
        conn = MagicMock()
        conn.cursor.return_value = cursor
        Import = self.env['ksw.bas.gl.import']
        with patch.object(type(Import), '_bas_connect', return_value=conn):
            return Import.action_import_cost_centres()

    def _acc(self, code):
        return self.env['account.analytic.account'].with_context(
            active_test=False).search([('x_bas_code', '=', code)])

    def _plan(self, code):
        return self.env['account.analytic.plan'].search([('x_bas_code', '=', code)])

    def test_tree_becomes_plans_and_accounts(self):
        res = self._run(self.TREE)
        self.assertEqual(res, {'plans': 4, 'accounts': 8, 'skipped': 0})
        root = self._plan('WREF10')
        self.assertFalse(root.parent_id)
        # group nodes are sub-plans under their BAS parent
        self.assertEqual(self._plan('1').parent_id, root)
        self.assertEqual(self._plan('14').parent_id, self._plan('1'))
        self.assertEqual(self._plan('141').parent_id, self._plan('14'))
        self.assertFalse(self._plan('11'), 'a leaf is not a plan')
        # a leaf sits in its parent's plan; a group's own account in its own
        self.assertEqual(self._acc('1410002').plan_id, self._plan('141'))
        self.assertEqual(self._acc('141').plan_id, self._plan('141'))
        self.assertEqual(self._acc('2').plan_id, self._plan('2'))
        # all under the one root, i.e. one analytic column
        self.assertEqual(self._acc('1410003').root_plan_id, root)
        self.assertEqual(self._acc('21').root_plan_id, root)
        self.assertEqual(self._acc('1410002').code, '1410002')
        self.assertEqual(self._acc('1').name, 'مراكز تكلفة شركة الكوثر')

    def test_rerun_never_touches_what_odoo_changed(self):
        self._run(self.TREE)
        self._acc('11').name = 'Administration (renamed in Odoo)'
        self._acc('1410003').action_archive()
        res = self._run(self.TREE)
        self.assertEqual(res, {'plans': 0, 'accounts': 0, 'skipped': 8})
        self.assertEqual(self._acc('11').name, 'Administration (renamed in Odoo)')
        self.assertEqual(len(self._acc('1410003')), 1)
        self.assertFalse(self._acc('1410003').active, 'archived stays archived')

    def test_rerun_adds_new_bas_codes_only(self):
        self._run(self.TREE)
        res = self._run(self.TREE + _wref(
            ('1410004', '141', 'T155'),
            ('1411', '141', 'تريلات مستاجرة'),
            ('1411001', '1411', 'T900'),
        ))
        self.assertEqual(res, {'plans': 1, 'accounts': 3, 'skipped': 8})
        self.assertEqual(self._acc('1410004').plan_id, self._plan('141'))
        self.assertEqual(self._plan('1411').parent_id, self._plan('141'))
        self.assertEqual(self._acc('1411001').plan_id, self._plan('1411'))

    def test_account_made_in_odoo_needs_no_bas_code(self):
        self._run(self.TREE)
        acc = self.env['account.analytic.account'].create({
            'name': 'New site', 'code': 'N-1', 'plan_id': self._plan('14').id})
        self.assertFalse(acc.x_bas_code)
        self.assertEqual(acc.root_plan_id, self._plan('WREF10'))
        self.assertEqual(self._run(self.TREE)['accounts'], 0)


@tagged('post_install', '-at_install')
class TestBasCostCentresOnLedger(_Isolated):
    """The ledger fill: match each line to its vou10 row inside the voucher."""

    WREF = _wref(
        ('1', '', 'الكوثر'),
        ('14', '1', 'السيارات'),
        ('1410002', '14', 'T153'),
        ('1410003', '14', 'T154'),
        ('19', '1', 'سائقين التريلات'),
        ('190001', '19', 'WAHAB JAN1387'),
        ('11', '1', 'الادارة'),
    )

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        A = cls.env['account.account']
        cls.exp = A.create({'code': 'T3101', 'name': 'Fuel', 'account_type': 'expense',
                            'x_bas_code': '3101'})
        cls.cash = A.create({'code': 'T1201', 'name': 'Cash', 'account_type': 'asset_cash',
                             'x_bas_code': '1201'})
        cls.journal = cls.env['account.journal'].create(
            {'name': 'BAS T', 'code': 'BAST', 'type': 'general'})

    def _connect(self, *results):
        cursor = MagicMock()
        cursor.fetchall.side_effect = list(results)
        conn = MagicMock()
        conn.cursor.return_value = cursor
        return patch.object(type(self.env['ksw.bas.gl.import']), '_bas_connect',
                            return_value=conn)

    def _seed(self):
        with self._connect(self.WREF):
            self.env['ksw.bas.gl.import'].action_import_cost_centres()

    def _move(self, number, lines):
        move = self.env['account.move'].create({
            'move_type': 'entry', 'journal_id': self.journal.id, 'date': '2026-03-10',
            'x_bas_key': f'018/0/010/{number}',
            'line_ids': [(0, 0, {'account_id': a.id, 'debit': d, 'credit': c, 'name': '/'})
                         for a, d, c in lines],
        })
        move.action_post()
        return move

    @staticmethod
    def _row(number, fcode, tcode, amount, cc1='', cc2='', cc3=''):
        return {'FTYPE': '018', 'FTYPE2': 0, 'CODE2': '010', 'NUMBER1': float(number),
                'FCODE': fcode, 'TCODE': tcode, 'AMOUNT': amount, 'BAMOUNT': 0.0,
                'cc1': cc1, 'cc2': cc2, 'cc3': cc3}

    def _acc(self, code, root):
        return self.env['account.analytic.account'].search([
            ('x_bas_code', '=', code), ('root_plan_id.x_bas_code', '=', root)])

    def _apply(self, rows):
        with self._connect(self.WREF, rows):
            return self.env['ksw.bas.gl.import'].action_apply_cost_centres(
                '2026-03-10', '2026-03-10', commit=False)

    def test_line_gets_its_own_row_vehicle_and_driver(self):
        self._seed()
        move = self._move(1, [(self.exp, 300, 0), (self.exp, 100, 0), (self.cash, 0, 400)])
        res = self._apply([
            self._row(1, '3101', '', 300, cc1='T153 ', cc2='WAHAB  JAN1387'),
            self._row(1, '3101', '', 100, cc1='', cc3='الادارة'),
            self._row(1, '', '1201', 400),
        ])
        l300, l100, lcash = move.line_ids.sorted('id')
        truck = self._acc('1410002', 'WREF10')
        driver = self._acc('190001', 'WREF10-EMP')
        self.assertTrue(driver, 'driver account made on demand in the employee plan')
        self.assertEqual(l300.analytic_distribution, {f'{truck.id},{driver.id}': 100.0})
        self.assertEqual(l100.analytic_distribution,
                         {str(self._acc('11', 'WREF10').id): 100.0}, 'COST_CENTER3 fallback')
        self.assertFalse(lcash.analytic_distribution, 'untagged in BAS stays untagged')
        self.assertEqual(res['lines'], 2)
        # one analytic line carries both columns, no double count
        self.assertEqual(len(l300.analytic_line_ids), 1)
        self.assertEqual(l300.analytic_line_ids.amount, -300)

    def test_merged_line_is_split_in_proportion(self):
        self._seed()
        move = self._move(2, [(self.exp, 400, 0), (self.cash, 0, 400)])
        self._apply([
            self._row(2, '3101', '', 300, cc1='T153'),
            self._row(2, '3101', '', 100, cc1='T154'),
            self._row(2, '', '1201', 400),
        ])
        t153, t154 = self._acc('1410002', 'WREF10'), self._acc('1410003', 'WREF10')
        self.assertEqual(move.line_ids.sorted('id')[0].analytic_distribution,
                         {str(t153.id): 75.0, str(t154.id): 25.0})

    def test_existing_distribution_is_kept(self):
        self._seed()
        move = self._move(3, [(self.exp, 50, 0), (self.cash, 0, 50)])
        mine = self._acc('1410003', 'WREF10')
        line = move.line_ids.sorted('id')[0]
        line.analytic_distribution = {str(mine.id): 100.0}
        self._apply([self._row(3, '3101', '', 50, cc1='T153'),
                     self._row(3, '', '1201', 50)])
        self.assertEqual(line.analytic_distribution, {str(mine.id): 100.0})

    def test_unknown_name_is_reported_not_guessed(self):
        self._seed()
        move = self._move(4, [(self.exp, 10, 0), (self.cash, 0, 10)])
        res = self._apply([self._row(4, '3101', '', 10, cc3='مستودع القدية الرياض'),
                           self._row(4, '', '1201', 10)])
        self.assertFalse(move.line_ids.sorted('id')[0].analytic_distribution)
        self.assertEqual(res['unknown_names'], {'مستودع القدية الرياض': 1})
