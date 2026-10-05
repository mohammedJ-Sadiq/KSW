from datetime import date

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPlDepreciationDrilldown(TransactionCase):
    def test_drilldown_lists_sub_accounts_summing_to_cell(self):
        """Clicking a depreciation figure lists its expense sub-accounts, and
        they add up to the figure (it used to open an empty journal list)."""
        Asset = self.env['ksw.bas.fixed.asset']
        company = self.env.company
        acc = self.env['account.account']
        exp_a = acc.create({'name': 'Dep A', 'code': '33029991', 'account_type': 'expense_depreciation'})
        exp_b = acc.create({'name': 'Dep B', 'code': '33029992', 'account_type': 'expense_depreciation'})
        fixed = acc.create({'name': 'Asset', 'code': '19029991', 'account_type': 'asset_fixed'})
        common = {'year': 2026, 'date': date(2026, 1, 1), 'asset_account_id': fixed.id,
                  'expense_group': '3302', 'rate': 10.0, 'accum_open': 0.0,
                  'company_id': company.id}
        Asset.create([
            dict(common, code='T1', expense_account_id=exp_a.id, cost=36500.0),
            dict(common, code='T2', expense_account_id=exp_b.id, cost=73000.0),
        ])
        wiz = self.env['ksw.pl.wizard'].create({
            'date_from': date(2026, 1, 1), 'date_to': date(2026, 1, 10)})
        instance = self.env['mis.report.instance'].browse(wiz.action_open()['res_id'])
        kpi = self.env.ref('KSW_accounting_ux.mis_pl_kpi_dep_3302')
        action = instance.drilldown({
            'period_id': instance.period_ids.id, 'kpi_id': kpi.id,
            'expr': kpi.expression})
        self.assertEqual(action['res_model'], 'ksw.pl.depreciation.line')
        lines = self.env['ksw.pl.depreciation.line'].search(action['domain'])
        all_3302 = Asset.search([('expense_group', '=', '3302'), ('company_id', '=', company.id)])
        self.assertAlmostEqual(sum(lines.mapped('amount')),
                               all_3302._bas_charge(date(2026, 1, 1), date(2026, 1, 10)), places=2)
        by_account = {l.account_id: l.amount for l in lines}
        self.assertAlmostEqual(by_account[exp_a], 36500 * 0.10 / 365 * 10, places=2)
        self.assertAlmostEqual(by_account[exp_b], 73000 * 0.10 / 365 * 10, places=2)

    def test_drilldown_opens_next_level(self):
        """From a level-3 line (3302) the list groups by level 4 (330201...)."""
        wiz = self.env['ksw.pl.wizard'].create({
            'date_from': date(2026, 1, 1), 'date_to': date(2026, 6, 30)})
        instance = self.env['mis.report.instance'].browse(wiz.action_open()['res_id'])
        kpi = self.env.ref('KSW_accounting_ux.mis_pl_kpi_dep_3306')
        action = instance.drilldown({
            'period_id': instance.period_ids.id, 'kpi_id': kpi.id, 'expr': kpi.expression})
        lines = self.env['ksw.pl.depreciation.line'].search(action['domain'])
        if not lines:
            self.skipTest('no BAS fixed assets loaded for 2026')
        self.assertEqual(action['context']['group_by'], ['x_group_l4_id', 'account_id'])
        for line in lines:
            self.assertEqual(line.x_group_l4_id.code_prefix_start, line.account_id.code[:6])
            self.assertEqual(line.x_group_l3_id.code_prefix_start, '3306')

    def test_journal_drilldown_opens_next_level(self):
        """Any MIS cell on journal items opens one chart level below it."""
        wiz = self.env['ksw.pl.wizard'].create({
            'date_from': date(2026, 1, 1), 'date_to': date(2026, 6, 30)})
        instance = self.env['mis.report.instance'].browse(wiz.action_open()['res_id'])
        period = instance.period_ids
        # level 2 '41': revenue -> Level 3, Level 4, Account
        action = instance.drilldown({'period_id': period.id, 'expr': '-balp[41%]'})
        if not self.env['account.move.line'].search_count(action['domain']):
            self.skipTest('no revenue posted in 2026')
        self.assertEqual(action['context']['group_by'],
                         ['x_group_l3_id', 'x_group_l4_id', 'account_id'])
        # one account: plain list of its entries
        line = self.env['account.move.line'].search(action['domain'], limit=1)
        action = instance.drilldown({'period_id': period.id, 'expr': '-balp[41%]',
                                     'account_id': line.account_id.id})
        self.assertNotIn('group_by', action.get('context') or {})

    def test_account_levels_follow_chart(self):
        account = self.env['account.account'].search([('code', '=like', '3302%')], limit=1)
        if not account:
            self.skipTest('BAS chart not loaded')
        self.assertEqual(
            [g.code_prefix_start for g in (account.x_group_l1_id, account.x_group_l2_id,
                                           account.x_group_l3_id, account.x_group_l4_id)],
            ['3', '33', '3302', account.code[:6]])

    def test_new_group_reparents_its_accounts(self):
        company = self.env.company
        top = self.env['account.group'].create(
            {'name': 'T', 'code_prefix_start': '97', 'company_id': company.id})
        account = self.env['account.account'].create(
            {'name': 'Leaf', 'code': '97010001', 'account_type': 'expense'})
        self.assertEqual(account.x_group_l1_id, top)
        self.assertFalse(account.x_group_l2_id)
        mid = self.env['account.group'].create(
            {'name': 'M', 'code_prefix_start': '9701', 'parent_id': top.id, 'company_id': company.id})
        self.assertEqual(account.x_group_l2_id, mid)
        mid.unlink()
        self.assertFalse(account.x_group_l2_id)
