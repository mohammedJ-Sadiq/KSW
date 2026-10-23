from odoo.exceptions import UserError
from odoo.tests import TransactionCase, new_test_user, tagged


@tagged('post_install', '-at_install')
class TestPurchaseLink(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.stock_acc = cls.env['account.account'].create({
            'name': 'Test tyres stock', 'code': '1299990001', 'account_type': 'asset_current'})
        cls.categ = cls.env['product.category'].create({
            'name': 'Test Tyres', 'property_valuation': 'real_time',
            'property_stock_valuation_account_id': cls.stock_acc.id})
        Product = cls.env['product.product']
        cls.gt = Product.create({'name': 'كفرات جي تي 315/80', 'is_storable': True,
                                 'type': 'consu', 'categ_id': cls.categ.id})
        cls.magna = Product.create({'name': 'كفر ميجانا 315/80', 'is_storable': True,
                                    'type': 'consu', 'categ_id': cls.categ.id})
        cls.spider = Product.create({'name': 'كفر لاند سبايدر 315/80', 'is_storable': True,
                                     'type': 'consu', 'categ_id': cls.categ.id})
        cls.vendor = cls.env['res.partner'].create({'name': 'التميمي'})
        cls.manager = new_test_user(cls.env, 'pl_manager',
                                    groups='base.group_user,stock.group_stock_manager')
        cls.stock_user = new_test_user(cls.env, 'pl_user', groups='base.group_user,stock.group_stock_user')
        cls.Review = cls.env['ksw.bas.purchase.review']

    def _bill(self, text, amount):
        bill = self.env['account.move'].create({
            'move_type': 'in_invoice', 'partner_id': self.vendor.id,
            'invoice_date': '2026-02-08',
            'invoice_line_ids': [(0, 0, {'name': text, 'quantity': 1, 'price_unit': amount,
                                         'account_id': self.stock_acc.id, 'tax_ids': [(6, 0, [])]})],
        })
        bill.action_post()
        return bill

    def _review(self, bill):
        self.Review.with_user(self.manager).action_collect()
        return self.Review.search([('move_id', '=', bill.id)])

    def test_suggest_and_confirm_keeps_the_bill_balance(self):
        bill = self._bill('عدد10كفر جي تي315/80 التميمي ف90859393', 9680.0)
        before = [(l.account_id.id, l.balance) for l in bill.line_ids]
        review = self._review(bill)
        self.assertEqual(review.product_id, self.gt)
        self.assertEqual(review.quantity, 10)
        self.assertEqual(review.unit_price, 968.0)
        review.with_user(self.manager).action_confirm()
        line = review.line_id
        self.assertEqual(line.product_id, self.gt)
        self.assertEqual(line.quantity, 10)
        self.assertAlmostEqual(line.price_unit, 968.0)
        self.assertEqual(bill.state, 'posted')
        self.assertEqual([(l.account_id.id, l.balance) for l in bill.line_ids], before)
        self.assertEqual(self.gt.x_vendor_bill_count, 1)
        self.assertEqual(self.gt.product_tmpl_id.x_vendor_bill_count, 1)
        self.assertEqual(self.gt.qty_available, 0, 'no stock move is created')
        action = self.gt.action_view_vendor_bill_history()
        self.assertEqual(self.env['ksw.product.purchase.history'].search(action['domain']).quantity, 10)

    def test_learned_description_suggests_the_same_item(self):
        first = self._review(self._bill('عدد10كفر ماجنا315/80 الثعبان الاسود ف6251', 9500.0))
        # «ماجنا» on the bill, «ميجانا» on the item: close enough to suggest.
        self.assertEqual(first.product_id, self.magna)
        first.with_user(self.manager).action_confirm()
        second = self._review(self._bill('عدد 20كفر ماجنا315/80 الثعبان الاسود ف7953', 19000.0))
        self.assertEqual(second.product_id, self.magna)
        self.assertEqual(second.quantity, 20)

    def test_split_line(self):
        bill = self._bill('عدد10كفر ماجنا315/80+40كفر لاند سبايدر315/80 تبوك', 50000.0)
        review = self._review(bill)
        self.assertFalse(review.product_id)
        review.part_ids = [(0, 0, {'product_id': self.magna.id, 'quantity': 10, 'amount': 10000.0}),
                           (0, 0, {'product_id': self.spider.id, 'quantity': 40, 'amount': 30000.0})]
        with self.assertRaises(UserError):
            review.with_user(self.manager).action_confirm()   # 40,000 of 50,000
        review.part_ids.filtered(lambda p: p.product_id == self.spider).amount = 40000.0
        review.with_user(self.manager).action_confirm()
        self.assertEqual(review.state, 'linked')
        self.assertFalse(review.line_id.product_id, 'a split line stays one line on the bill')
        self.assertEqual(self.spider.x_vendor_bill_count, 1)
        self.assertEqual(self.magna.x_vendor_bill_count, 1)

    def test_undo_restores_the_bill_line(self):
        review = self._review(self._bill('عدد10كفر جي تي315/80 ف1', 9680.0))
        review.with_user(self.manager).action_confirm()
        review.with_user(self.manager).action_reset()
        self.assertEqual(review.state, 'review')
        self.assertFalse(review.line_id.product_id)
        self.assertEqual(review.line_id.quantity, 1)
        self.assertEqual(review.line_id.price_unit, 9680.0)
        self.assertEqual(self.gt.x_vendor_bill_count, 0)

    def test_not_stock_and_collect_once(self):
        bill = self._bill('عدد 5 ماكينة ثلج مشروع القدية ف 2561', 5000.0)
        review = self._review(bill)
        review.with_user(self.manager).action_not_stock()
        self.assertEqual(review.state, 'not_stock')
        self.assertEqual(len(self._review(bill)), 1, 'a line is listed once')

    def test_only_inventory_admin_links(self):
        review = self._review(self._bill('عدد10كفر جي تي315/80 ف2', 9680.0))
        with self.assertRaises(UserError):
            review.with_user(self.stock_user).action_confirm()


@tagged('post_install', '-at_install')
class TestOnePurchaseHistory(TestPurchaseLink):
    """Both buttons carry the BAS history and carry on with Odoo's own records."""

    def _history(self, action):
        return self.env['ksw.product.purchase.history'].search(action['domain'])

    def test_both_buttons_show_bas_then_odoo(self):
        review = self._review(self._bill('عدد10كفر جي تي315/80 التميمي ف90859393', 9680.0))
        review.with_user(self.manager).action_confirm()
        # A purchase made in Odoo afterwards: order, then its bill.
        order = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'order_line': [(0, 0, {'product_id': self.gt.id, 'product_qty': 4, 'price_unit': 900.0,
                                   'tax_ids': [(6, 0, [])]})],
        })
        order.button_confirm()
        bill = self.env['account.move'].create({
            'move_type': 'in_invoice', 'partner_id': self.vendor.id, 'invoice_date': '2026-10-01',
            'invoice_line_ids': [(0, 0, {'product_id': self.gt.id, 'quantity': 4, 'price_unit': 900.0,
                                         'tax_ids': [(6, 0, [])]})],
        })
        bill.action_post()

        purchased = self._history(self.gt.action_view_po())
        self.assertEqual(sorted(purchased.mapped('kind')), ['bas', 'order'])
        self.assertEqual(sum(purchased.mapped('quantity')), 14)
        self.assertEqual(self.gt.purchased_product_qty, 14, 'the Purchased figure counts BAS too')

        billed = self._history(self.gt.action_view_vendor_bill_history())
        # The BAS line has its product written on the bill line: listed once, as BAS.
        self.assertEqual(sorted(billed.mapped('kind')), ['bas', 'bill'])
        self.assertEqual(sum(billed.mapped('quantity')), 14)
        self.assertEqual(self.gt.x_vendor_bill_count, 2)
        self.assertEqual(self.gt.product_tmpl_id.x_vendor_bill_count, 2)
        self.assertEqual(self._history(self.gt.product_tmpl_id.action_view_po()), purchased)
