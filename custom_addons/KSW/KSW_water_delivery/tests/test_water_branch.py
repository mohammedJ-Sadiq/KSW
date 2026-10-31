"""The branch master is the warehouse (Oct 2026)."""
from odoo.exceptions import UserError
from odoo.tests import tagged

from .test_water_delivery import WaterDeliveryCommon


@tagged('post_install', '-at_install')
class TestWaterBranch(WaterDeliveryCommon):

    def test_branch_settings_live_on_the_warehouse(self):
        warehouse = self.picking_type.warehouse_id
        till = self.env['account.journal'].create({'name': 'Br Till', 'code': 'BRTL', 'type': 'cash'})
        self.picking_type.write({'x_branch_code': '555', 'x_cash_journal_id': till.id})
        self.assertEqual(warehouse.x_branch_code, '555')
        self.assertEqual(warehouse.x_cash_journal_id, till)
        warehouse.x_branch_code = '556'
        self.assertEqual(self.picking_type.x_branch_code, '556', 'one value, two views')

    def test_a_note_leaves_from_its_branch_stock(self):
        note = self._issue_note().picking_id.sudo()
        self.assertEqual(note.location_id, self.picking_type.default_location_src_id)
        self.assertEqual(note.x_branch_code, self.picking_type.warehouse_id.x_branch_code)

    def test_delivering_branches_on_the_customer(self):
        warehouse = self.picking_type.warehouse_id
        partner = self.env['res.partner'].create({'name': 'Branch Client', 'customer_rank': 1})
        partner.x_water_branch_ids = warehouse
        rows = self.env['ksw.water.client.branch'].search([('partner_id', '=', partner.id)])
        self.assertEqual(rows.branch_code, warehouse.x_branch_code)
        self.assertEqual(rows.warehouse_id, warehouse)
        partner.x_water_branch_ids = False
        self.assertFalse(rows.active, 'removed from the customer, kept as history')
        partner.x_water_branch_ids = warehouse
        self.assertTrue(rows.active)
        self.assertEqual(len(self.env['ksw.water.client.branch'].with_context(
            active_test=False).search([('partner_id', '=', partner.id)])), 1)


@tagged('post_install', '-at_install')
class TestRevenueAccountDetermination(WaterDeliveryCommon):

    def _income(self, code):
        return self.env['account.account'].create({
            'name': f'Sales {code}', 'code': code, 'account_type': 'income'})

    def test_the_branch_is_the_key_and_its_code_follows(self):
        warehouse = self.picking_type.warehouse_id
        self.picking_type.x_branch_code = '561'
        line_account = self._income('400561')
        rule = self.env['ksw.water.revenue.account'].create({
            'warehouse_id': warehouse.id, 'product_id': self.water.id,
            'account_id': line_account.id})
        self.assertEqual(rule.branch_code, '561')
        Rev = self.env['ksw.water.revenue.account']
        self.assertEqual(Rev._account_for_branch(self.picking_type, self.water), line_account)

    def test_a_product_without_a_line_takes_the_branch_default(self):
        default = self._income('400562')
        self.picking_type.write({'x_branch_code': '562', 'x_sales_account_id': default.id})
        Rev = self.env['ksw.water.revenue.account']
        self.assertEqual(Rev._account_for_branch(self.picking_type, self.water), default)

    def test_a_branch_without_a_code_is_refused(self):
        warehouse = self.env['stock.warehouse'].create({'name': 'No Code WH', 'code': 'NCW'})
        with self.assertRaises(UserError):
            self.env['ksw.water.revenue.account'].create({
                'warehouse_id': warehouse.id, 'product_id': self.water.id})

    def test_it_lives_under_accounting_configuration(self):
        menu = self.env.ref('KSW_water_delivery.menu_water_delivery_revenue_accounts')
        self.assertEqual(menu.parent_id, self.env.ref('account.account_account_menu'))
        self.assertEqual(menu.group_ids, self.env.ref('account.group_account_manager'))
