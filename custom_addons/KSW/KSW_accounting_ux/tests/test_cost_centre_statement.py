from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestCostCentreStatement(TransactionCase):

    def test_statement_shares_opening_and_running_balance(self):
        Import = self.env['ksw.bas.gl.import']
        emp_plan = Import._employee_root_plan()
        Account = self.env['account.analytic.account']
        driver = Account.create({'name': 'DRIVER ZZ 1', 'plan_id': emp_plan.id})
        other = Account.create({'name': 'DRIVER ZZ 2', 'plan_id': emp_plan.id})
        expense = self.env['account.account'].create({
            'name': 'ZZ expense', 'code': '3999990011', 'account_type': 'expense'})
        cash = self.env['account.account'].create({
            'name': 'ZZ cash', 'code': '1999990011', 'account_type': 'asset_current'})

        def entry(day, amount, dist):
            m = self.env['account.move'].create({'move_type': 'entry', 'date': day, 'line_ids': [
                (0, 0, {'account_id': expense.id, 'debit': amount, 'analytic_distribution': dist}),
                (0, 0, {'account_id': cash.id, 'credit': amount})]})
            m.action_post()
            return m

        entry('2026-11-05', 100.0, {str(driver.id): 100})                       # before: opening
        first = entry('2026-11-10', 200.0, {f'{driver.id}': 60, str(other.id): 40})  # 60% ours
        entry('2026-12-01', 50.0, {str(driver.id): 100})
        entry('2026-12-02', 999.0, {str(other.id): 100})                        # not ours

        wiz = self.env['ksw.cost.centre.statement'].create({
            'plan_filter': 'employee', 'analytic_account_id': driver.id,
            'date_from': '2026-11-08', 'date_to': '2026-12-31'})
        action = wiz.action_show()
        lines = self.env['ksw.cost.centre.statement.line'].search(action['domain'])
        self.assertEqual(lines.mapped('is_opening'), [True, False, False])
        self.assertEqual(wiz.opening_balance, 100.0)
        self.assertEqual(lines[1].move_id, first)
        self.assertEqual(lines[1].debit, 120.0, '60% of the 200 line')
        self.assertEqual(lines.mapped('running_balance'), [100.0, 220.0, 270.0])
        self.assertEqual(wiz.closing_balance, 270.0)
        self.assertEqual(lines[1].action_open_document()['res_id'], first.id)
        # P&L only drops nothing here; balance sheet only drops everything.
        wiz.account_scope = 'bs'
        wiz.action_show()
        self.assertEqual(len(wiz.line_ids), 1)
        # A BAS twin (same code, other plan) belongs to the same statement.
        cc_plan = Import._cost_centre_root_plan()
        driver.x_bas_code = 'ZZD1'
        twin = Account.create({'name': 'DRIVER ZZ 1', 'plan_id': cc_plan.id, 'x_bas_code': 'ZZD1'})
        entry('2026-12-03', 30.0, {str(twin.id): 100})
        wiz.write({'account_scope': 'all', 'analytic_account_id': twin.id, 'plan_filter': 'all'})
        wiz.action_show()
        self.assertEqual(wiz.closing_balance, 300.0)
        html, _fmt = self.env['ir.actions.report']._render_qweb_html(
            'KSW_accounting_ux.report_cost_centre_statement', wiz.ids)
        self.assertIn(b'DRIVER ZZ 1', html)


@tagged('post_install', '-at_install')
class TestCostCentresStatement(TransactionCase):
    """Several cost centres: by code range, by group, as statements or a summary."""

    def setUp(self):
        super().setUp()
        Import = self.env['ksw.bas.gl.import']
        root = Import._cost_centre_root_plan()
        Plan, Account = self.env['account.analytic.plan'], self.env['account.analytic.account']
        self.group = Plan.create({'name': 'ZZ Trailers', 'parent_id': root.id})
        self.t1 = Account.create({'name': 'ZT1', 'code': '9941001', 'plan_id': self.group.id})
        self.t2 = Account.create({'name': 'ZT2', 'code': '9941002', 'plan_id': self.group.id})
        self.t3 = Account.create({'name': 'ZT3', 'code': '9942001', 'plan_id': root.id})
        expense = self.env['account.account'].create({
            'name': 'ZZ tyres', 'code': '3999990021', 'account_type': 'expense'})
        cash = self.env['account.account'].create({
            'name': 'ZZ cash 2', 'code': '1999990021', 'account_type': 'asset_current'})
        for day, amount, acc in (('2026-11-05', 100, self.t1), ('2026-11-20', 40, self.t1),
                                 ('2026-11-21', 70, self.t2), ('2026-11-22', 5, self.t3)):
            m = self.env['account.move'].create({'move_type': 'entry', 'date': day, 'line_ids': [
                (0, 0, {'account_id': expense.id, 'debit': amount, 'analytic_distribution': {str(acc.id): 100}}),
                (0, 0, {'account_id': cash.id, 'credit': amount})]})
            m.action_post()

    def _wiz(self, **vals):
        return self.env['ksw.cost.centre.statement'].create(dict(
            {'plan_filter': 'cost_centre', 'date_from': '2026-11-10', 'date_to': '2026-12-31'}, **vals))

    def test_range_is_textual_and_statements_per_cost_centre(self):
        wiz = self._wiz(selection_mode='range', code_from='9941', code_to='9941')
        action = wiz.action_show()
        self.assertEqual(wiz.unit_count, 2, '9941 to 9941 covers 9941001 and 9941002, not 9942001')
        self.assertEqual(action['context']['group_by'], ['cost_centre_id'])
        t1 = wiz.line_ids.filtered(lambda l: l.cost_centre_id == self.t1)
        self.assertEqual(t1.mapped('running_balance'), [100.0, 140.0], 'opening 100, then +40')
        self.assertEqual(wiz.closing_balance, 210.0)

    def test_group_and_summary(self):
        wiz = self._wiz(selection_mode='group', group_plan_id=self.group.id, layout='summary')
        # The picker's own domain, as the browser runs it, must offer the group.
        Plan = self.env['account.analytic.plan']
        self.assertIn(self.group, Plan.search([('id', 'in', wiz.allowed_group_ids.ids)]))
        # ... and so must the unsaved form record the browser actually has.
        draft = self.env['ksw.cost.centre.statement'].new({'plan_filter': 'cost_centre'})
        self.assertIn(self.group.id, draft.allowed_group_ids._origin.ids)
        wiz.action_show()
        rows = {l.cost_centre_id: (l.opening, l.debit, l.running_balance) for l in wiz.line_ids}
        self.assertEqual(rows, {self.t1: (100.0, 40.0, 140.0), self.t2: (0.0, 70.0, 70.0)})
        # A summary row opens that cost centre's own statement for the same period.
        row = wiz.line_ids.filtered(lambda l: l.cost_centre_id == self.t2)
        action = row.action_open_cost_centre_statement()
        lines = self.env['ksw.cost.centre.statement.line'].search(action['domain'])
        self.assertEqual(lines.mapped('debit'), [0.0, 70.0])
        html, _fmt = self.env['ir.actions.report']._render_qweb_html(
            'KSW_accounting_ux.report_cost_centre_statement', wiz.ids)
        self.assertIn(b'ZT2', html)
