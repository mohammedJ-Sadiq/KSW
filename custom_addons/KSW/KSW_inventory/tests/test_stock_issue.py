from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestStockIssue(TransactionCase):
    """A stock issue posts like BAS 101: Dr issue account (+ cost centre) / Cr stock."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        Account = cls.env['account.account']
        cls.stock_acc = Account.create({'name': 'KSW test stock', 'code': '1206999901', 'account_type': 'asset_current'})
        cls.expense_acc = Account.create({'name': 'KSW test tyre expense', 'code': '3102999901', 'account_type': 'expense'})
        cls.driver_acc = Account.create({'name': 'KSW test driver debt', 'code': '1207999901', 'account_type': 'asset_current'})
        cls.categ = cls.env['product.category'].create({'name': 'KSW test tyres'})
        cls.categ.with_company(cls.company).write({
            'property_valuation': 'real_time', 'property_cost_method': 'average',
            'property_stock_valuation_account_id': cls.stock_acc.id,
            'property_account_expense_categ_id': cls.expense_acc.id,
        })
        cls.product = cls.env['product.product'].create({
            'name': 'KSW test tyre', 'default_code': '33999', 'is_storable': True,
            'categ_id': cls.categ.id, 'standard_price': 100.0, 'purchase_method': 'receive'})
        cls.consumption = cls.env.ref('KSW_inventory.location_consumption')
        if not cls.consumption.valuation_account_id:
            cls.consumption.valuation_account_id = cls.expense_acc
        cls.wh = cls.env['stock.warehouse'].create({'name': 'KSW test warehouse', 'code': 'KTW', 'company_id': cls.company.id})
        cls.issue_type = cls.env['stock.picking.type'].create({
            'name': 'Stock Issue', 'code': 'outgoing', 'sequence_code': 'ISS',
            'warehouse_id': cls.wh.id, 'default_location_src_id': cls.wh.lot_stock_id.id,
            'default_location_dest_id': cls.consumption.id, 'x_ksw_issue': True})
        plan = cls.env['account.analytic.plan'].create({'name': 'KSW test cost centres'})
        cls.cost_centre = cls.env['account.analytic.account'].create({'name': 'Truck 4689', 'plan_id': plan.id})
        cls.env['stock.quant']._update_available_quantity(cls.product, cls.wh.lot_stock_id, 10.0)

    def _issue(self, qty=3.0, account=None, analytic=True):
        picking = self.env['stock.picking'].create({
            'picking_type_id': self.issue_type.id,
            'location_id': self.wh.lot_stock_id.id, 'location_dest_id': self.consumption.id,
            'x_issue_account_id': account.id if account else False,
            'move_ids': [(0, 0, {
                'product_id': self.product.id, 'product_uom_qty': qty,
                'location_id': self.wh.lot_stock_id.id, 'location_dest_id': self.consumption.id,
                'analytic_distribution': {str(self.cost_centre.id): 100} if analytic else False,
            })],
        })
        picking.action_confirm()
        picking.move_ids.quantity = qty
        picking.move_ids.picked = True
        return picking

    def _entry(self, picking):
        move = picking.move_ids.account_move_id
        self.assertTrue(move, 'validating a stock issue must post a journal entry')
        self.assertEqual(move.state, 'posted')
        return {l.account_id: l for l in move.line_ids}

    def test_issue_posts_category_expense_with_cost_centre(self):
        picking = self._issue()
        picking.button_validate()
        lines = self._entry(picking)
        self.assertAlmostEqual(lines[self.expense_acc].debit, 300.0)
        self.assertAlmostEqual(lines[self.stock_acc].credit, 300.0)
        self.assertEqual(lines[self.expense_acc].analytic_distribution, {str(self.cost_centre.id): 100})
        self.assertFalse(lines[self.stock_acc].analytic_distribution)
        self.assertEqual(self.product.with_context(location=self.wh.lot_stock_id.id).qty_available, 7.0)

    def test_issue_account_override_charges_the_driver(self):
        picking = self._issue(account=self.driver_acc)
        picking.button_validate()
        lines = self._entry(picking)
        self.assertNotIn(self.expense_acc, lines)
        self.assertAlmostEqual(lines[self.driver_acc].debit, 300.0)
        self.assertAlmostEqual(lines[self.stock_acc].credit, 300.0)

    def test_issue_without_cost_centre_is_refused(self):
        picking = self._issue(analytic=False)
        with self.assertRaises(UserError):
            picking.button_validate()
        self.assertNotEqual(picking.state, 'done')

    def test_vendor_bill_debits_the_stock_account(self):
        """BAS 006: Dr 1206 stock + VAT / Cr supplier."""
        vendor = self.env['res.partner'].create({'name': 'KSW test tyre supplier', 'supplier_rank': 1})
        bill = self.env['account.move'].create({
            'move_type': 'in_invoice', 'partner_id': vendor.id, 'invoice_date': fields.Date.today(),
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 2, 'price_unit': 650.0})],
        })
        self.assertEqual(bill.invoice_line_ids.account_id, self.stock_acc)

    def test_bas_item_classification(self):
        classify = self.env['ksw.bas.inventory.setup']._classify
        self.assertEqual(classify('33556')[1][1], '1206010003')   # tyre
        self.assertEqual(classify('15003')[1][1], '1206010002')   # desalination part
        self.assertEqual(classify('31001')[1][1], '1206010001')   # membrane
        self.assertEqual(classify('11032')[0], 'water')
        self.assertEqual(classify('666')[0], 'service')
        self.assertEqual(classify('9001')[0], 'service')
        self.assertEqual(classify('XYZ'), (False, False))
