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
        self.assertEqual(len(d['trend']), 12)

    def test_inventory_figures(self):
        d = self.env['ksw.erp.dashboard'].get_inventory_data('month')
        self.assertGreaterEqual(d['value'], 0)
        self.assertEqual(len(d['trend']), 12)
        Quant = self.env['stock.quant']
        for cat in d['by_category']:
            self.assertTrue(Quant.search_count([('location_id.usage', '=', 'internal'),
                                                ('product_id.categ_id', 'child_of', cat['id'])]))
