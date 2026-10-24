from odoo import fields
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestErpDashboard(TransactionCase):

    def test_finance_figures_and_drilldowns(self):
        partner = self.env['res.partner'].create({'name': 'ZZ Customer'})
        income = self.env['account.account'].create({
            'name': 'ZZ sales', 'code': '4999990091', 'account_type': 'income'})
        inv = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': partner.id, 'invoice_date': '2026-11-01',
            'invoice_date_due': '2026-11-01',
            'invoice_line_ids': [(0, 0, {'name': 'x', 'quantity': 1, 'price_unit': 1000, 'account_id': income.id,
                                         'tax_ids': [(6, 0, [])]})]})
        inv.action_post()
        for period in ('month', 'last_month', 'quarter', 'year'):
            d = self.env['ksw.erp.dashboard'].get_finance_data(period)
            self.assertEqual(sum(b['amount'] for b in d['receivable']['buckets']), d['receivable']['open'])
        # Every bucket's domain finds exactly the amount it shows.
        AML = self.env['account.move.line']
        for b in d['receivable']['buckets']:
            self.assertAlmostEqual(sum(AML.search(b['domain']).mapped('amount_residual')), b['amount'], 2)
        self.assertIn(partner.display_name, [t['name'] for t in d['receivable']['top']] + [partner.display_name])
        self.assertEqual({t['key'] for t in d['todo']}, {'drafts', 'bills_due', 'invoices_overdue', 'payments_open'})
        # The chart covers the period on the page: by day, week or month.
        self.assertAlmostEqual(sum(t['revenue'] for t in d['trend']), d['pl']['current']['revenue'], 2)
        self.assertAlmostEqual(sum(t['expenses'] for t in d['trend']), d['pl']['current']['expenses'], 2)
        self.assertEqual((d['trend_unit'], len(d['trend'])), ('month', fields.Date.context_today(self.env['ksw.erp.dashboard']).month))
        for period, unit in (('month', 'day'), ('last_month', 'day'), ('quarter', 'week')):
            self.assertEqual(self.env['ksw.erp.dashboard'].get_finance_data(period)['trend_unit'], unit)
        d = self.env['ksw.erp.dashboard'].get_finance_data('month')
        self.assertEqual(len(d['trend']), fields.Date.context_today(self.env['ksw.erp.dashboard']).day)
        self.assertAlmostEqual(sum(t['revenue'] for t in d['trend']), d['pl']['current']['revenue'], 2)

    def test_inventory_figures(self):
        d = self.env['ksw.erp.dashboard'].get_inventory_data('month')
        self.assertGreaterEqual(d['value'], 0)
        self.assertEqual((d['trend_unit'], len(d['trend'])), ('day', fields.Date.context_today(self.env['ksw.erp.dashboard']).day))
        Quant = self.env['stock.quant']
        for cat in d['by_category']:
            self.assertTrue(Quant.search_count([('location_id.usage', '=', 'internal'),
                                                ('product_id.categ_id', 'child_of', cat['id'])]))

    def test_overviews_are_listed_in_the_dashboards_app(self):
        Dashboard = self.env['spreadsheet.dashboard']
        for xmlid, tag, group in (
                ('KSW_erp_dashboard.hub_dashboard_finance', 'ksw_finance_dashboard', 'account.group_account_basic'),
                ('KSW_erp_dashboard.hub_dashboard_inventory', 'ksw_inventory_dashboard', 'stock.group_stock_user')):
            entry = self.env.ref(xmlid)
            self.assertEqual(entry.x_client_action_tag, tag)
            self.assertTrue(entry.is_published)
            self.assertEqual(entry.group_ids, self.env.ref(group))
        # Seen only by the roles that open the overview's menu (the core ir.rule on group_ids).
        clerk = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'hub clerk', 'login': 'hub_clerk',
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id])]})
        keeper = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'hub keeper', 'login': 'hub_keeper',
            'group_ids': [(6, 0, [self.env.ref('stock.group_stock_user').id])]})
        inventory = self.env.ref('KSW_erp_dashboard.hub_dashboard_inventory')
        self.assertNotIn(inventory, Dashboard.with_user(clerk).search([]))
        self.assertIn(inventory, Dashboard.with_user(keeper).search([]))
        self.assertNotIn(self.env.ref('KSW_erp_dashboard.hub_dashboard_finance'), Dashboard.with_user(keeper).search([]))

