from datetime import datetime
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestHistoryReplay(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tyres = cls.env.ref('KSW_inventory.categ_tyres')
        Product = cls.env['product.product']
        cls.tyre = Product.create({'name': 'Test tyre 315/80', 'default_code': 'ZT001', 'is_storable': True,
                                   'type': 'consu', 'categ_id': cls.tyres.id, 'standard_price': 900.0})
        cls.bad = Product.create({'name': 'Test tyre B', 'default_code': 'ZT002', 'is_storable': True,
                                  'type': 'consu', 'categ_id': cls.tyres.id})
        cls.wh = cls.env['stock.warehouse'].create({
            'name': 'Test W99', 'code': 'W99', 'x_bas_code': '99', 'x_bas_kind': 'warehouse'})
        cls.env['stock.picking.type'].create({
            'name': 'Stock Issue', 'code': 'outgoing', 'sequence_code': 'ISS', 'warehouse_id': cls.wh.id,
            'default_location_src_id': cls.wh.lot_stock_id.id,
            'default_location_dest_id': cls.env.ref('KSW_inventory.location_consumption').id,
            'x_ksw_issue': True})
        # The opening load, as it is today: BAS's current stock of 4.
        cls.env['stock.quant'].with_context(inventory_mode=True).create({
            'product_id': cls.tyre.id, 'location_id': cls.wh.lot_stock_id.id,
            'inventory_quantity': 4}).action_apply_inventory()

    def _row(self, ftype, n, day, code, qty, cc=''):
        return {'FTYPE': ftype, 'FTYPE2': 0, 'CODE2': '199', 'N': n, 'SERIAL': 1,
                'FDATE': datetime(2026, day[0], day[1], 10, 0), 'ICODE': code, 'QUAN': float(qty),
                'PRICE': 950.0, 'AMOUNT': 950.0 * qty, 'FCODE': '', 'CC': cc}

    def _run(self):
        purchases = [self._row('006', 1, (2, 1), 'ZT001', 10), self._row('006', 2, (3, 1), 'ZT002', 9)]
        issues = [self._row('101', 3, (2, 10), 'ZT001', 2, 'تيدر رقم 161'),
                  self._row('101', 4, (4, 5), 'ZT001', 6, 'ت179'),
                  self._row('101', 5, (4, 6), 'ZT002', 5)]
        now = {('ZT001', '99'): 4.0, ('ZT002', '99'): 0.0}
        wizard = self.env['ksw.bas.inventory.setup'].create({'year': 2026})
        Cls = type(wizard)
        with patch.object(Cls, '_bas_item_lines', return_value=(purchases, issues)), \
                patch.object(Cls, '_bas_quantities_now', return_value=now), \
                patch.object(Cls, '_vehicle_cost_centres', return_value={}):
            wizard.action_rebuild_movement_history()
        return wizard

    def test_history_added_stock_unchanged_nothing_posted(self):
        wizard = self._run()
        self.assertEqual(self.tyre.with_context(location=self.wh.lot_stock_id.id).qty_available, 4)
        moves = self.env['stock.move'].search([('product_id', '=', self.tyre.id), ('state', '=', 'done')],
                                              order='date, id')
        self.assertFalse(moves.filtered('account_move_id'), 'nothing posted')
        # Opening balance = issues 8 - purchases 10 = -2, on top of the opening load of 4.
        opening = moves.filtered(lambda m: m.inventory_name == 'BAS opening balance 2026')
        self.assertEqual(opening.quantity, 2)
        self.assertEqual(opening.location_id, self.wh.lot_stock_id, 'a negative balance leaves the stock')
        receipt = moves.filtered(lambda m: m.picking_id.x_bas_key == '006/0/199/1')
        self.assertEqual(str(receipt.date.date()), '2026-02-01')
        self.assertEqual(receipt.quantity, 10)
        issue = moves.filtered(lambda m: m.picking_id.x_bas_key == '101/0/199/3')
        self.assertEqual(str(issue.date.date()), '2026-02-10')
        self.assertTrue(issue.location_dest_id.x_ksw_consumption)
        self.assertIn('تيدر رقم 161', issue.picking_id.origin)
        self.assertEqual(self.tyre.standard_price, 900.0, 'unit cost put back')
        self.assertEqual(self.tyres.property_valuation, 'real_time')
        # The item whose BAS figures do not add up (0 now, 9 in, 5 out: 4 left with no document) is refused, not invented.
        self.assertIn('ZT002', wizard.log)
        self.assertFalse(self.env['stock.move'].search([('product_id', '=', self.bad.id),
                                                        ('picking_id', '!=', False)]))

    def test_second_run_changes_nothing(self):
        self._run()
        count = self.env['stock.move'].search_count([('product_id', '=', self.tyre.id)])
        self._run()
        self.assertEqual(self.env['stock.move'].search_count([('product_id', '=', self.tyre.id)]), count)


@tagged('post_install', '-at_install')
class TestReceiptVendorBill(TestHistoryReplay):

    def test_receipt_opens_its_bill_and_gets_the_vendor_of_an_entry(self):
        payable = self.env['account.account'].create({
            'name': 'Supplier X', 'code': '2101999901', 'account_type': 'liability_payable',
            'reconcile': True})
        vendor = self.env['res.partner'].create({'name': 'Vendor X'})
        stock_acc = self.tyres.property_stock_valuation_account_id
        # An earlier bill tells which vendor the supplier account belongs to.
        bill = self.env['account.move'].create({
            'move_type': 'in_invoice', 'partner_id': vendor.id, 'invoice_date': '2026-01-15',
            'invoice_line_ids': [(0, 0, {'name': 'x', 'quantity': 1, 'price_unit': 10,
                                         'account_id': stock_acc.id, 'tax_ids': [(6, 0, [])]})]})
        bill.line_ids.filtered(lambda l: l.display_type == 'payment_term').account_id = payable
        bill.action_post()
        # The BAS purchase itself, booked as a journal entry with no partner.
        entry = self.env['account.move'].create({
            'move_type': 'entry', 'date': '2026-02-01', 'x_bas_key': '006/0/199/1',
            'line_ids': [(0, 0, {'account_id': stock_acc.id, 'debit': 9500.0}),
                         (0, 0, {'account_id': payable.id, 'credit': 9500.0})]})
        entry.action_post()
        self._run()
        picking = self.env['stock.picking'].search([('x_bas_key', '=', '006/0/199/1')])
        self.assertEqual(picking.x_vendor_bill_id, entry)
        self.assertEqual(picking.x_vendor_bill_count, 1)
        self.assertEqual(picking.partner_id, vendor)
        action = picking.action_view_vendor_bills()
        self.assertEqual((action['res_model'], action['res_id']), ('account.move', entry.id))
        # ... and the way back, from the entry to its receipt.
        self.assertEqual(entry.x_picking_ids, picking)
        back = entry.action_view_ksw_pickings()
        self.assertEqual((back['res_model'], back['res_id']), ('stock.picking', picking.id))
