from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestStockMovement(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        stock = cls.env.ref('stock.stock_location_stock')
        Location = cls.env['stock.location']
        cls.loc_a = Location.create({'name': 'KSW Test A', 'usage': 'internal',
                                     'location_id': stock.id})
        cls.loc_b = Location.create({'name': 'KSW Test B', 'usage': 'internal',
                                     'location_id': stock.id})
        cls.supplier = cls.env.ref('stock.stock_location_suppliers')
        cls.customer = cls.env.ref('stock.stock_location_customers')
        cls.product = cls.env['product.product'].create({
            'name': 'KSW Test Water', 'type': 'consu', 'is_storable': True})
        cls.other = cls.env['product.product'].create({
            'name': 'KSW Test Other', 'type': 'consu', 'is_storable': True})

    def _move(self, src, dest, qty, product=None, days_ago=0, done=True):
        product = product or self.product
        move = self.env['stock.move'].create({
            'product_id': product.id, 'product_uom': product.uom_id.id,
            'product_uom_qty': qty, 'location_id': src.id, 'location_dest_id': dest.id,
        })
        move._action_confirm()
        if not done:
            return move
        move._action_assign()
        move.quantity = qty
        move.picked = True
        move._action_done()
        if days_ago:
            when = fields.Datetime.now() - timedelta(days=days_ago)
            move.write({'date': when})
            move.move_line_ids.write({'date': when})
        return move

    def _rows(self, **vals):
        wizard = self.env['ksw.stock.movement.wizard'].create(dict(
            {'location_ids': [(6, 0, self.loc_a.ids)], 'product_ids': [(6, 0, self.product.ids)]},
            **vals))
        action = wizard.action_show()
        return wizard.line_ids, action

    def test_in_and_out_give_the_closing_balance(self):
        self._move(self.supplier, self.loc_a, 10.0)
        self._move(self.loc_a, self.customer, 4.0)
        rows, _action = self._rows(show_opening=False)
        self.assertEqual(sorted(rows.mapped('kind')), ['in', 'out'])
        self.assertEqual(sum(rows.mapped('qty_in')), 10.0)
        self.assertEqual(sum(rows.mapped('qty_out')), 4.0)
        self.assertEqual(sum(rows.mapped('qty_net')), 6.0)
        out = rows.filtered(lambda r: r.kind == 'out')
        self.assertEqual(out.location_id, self.loc_a)
        self.assertEqual(out.other_location_id, self.customer)

    def test_a_transfer_between_two_chosen_stocks_is_two_rows(self):
        self._move(self.loc_a, self.loc_b, 3.0)
        rows, _action = self._rows(
            location_ids=[(6, 0, (self.loc_a | self.loc_b).ids)], show_opening=False)
        out = rows.filtered(lambda r: r.kind == 'out')
        in_ = rows.filtered(lambda r: r.kind == 'in')
        self.assertEqual((out.location_id, out.qty_net), (self.loc_a, -3.0))
        self.assertEqual((in_.location_id, in_.qty_net), (self.loc_b, 3.0))
        self.assertEqual(sum(rows.mapped('qty_net')), 0.0)

    def test_the_opening_row_carries_what_moved_before_the_period(self):
        self._move(self.supplier, self.loc_a, 7.0, days_ago=40)
        self._move(self.loc_a, self.customer, 2.0)
        rows, _action = self._rows(show_opening=True)
        opening = rows.filtered(lambda r: r.kind == 'opening')
        self.assertEqual(opening.qty_net, 7.0)
        self.assertEqual(opening.date, fields.Date.context_today(self.env.user).replace(day=1))
        self.assertEqual(sum(rows.mapped('qty_net')), 5.0, 'opening + in - out = closing')

    def test_pending_moves_only_when_asked(self):
        self._move(self.supplier, self.loc_a, 9.0)
        self._move(self.loc_a, self.customer, 5.0, done=False)
        rows, _action = self._rows(show_opening=False)
        self.assertFalse(rows.filtered(lambda r: r.state == 'pending'))
        rows, _action = self._rows(show_opening=False, include_pending=True)
        pending = rows.filtered(lambda r: r.state == 'pending')
        self.assertEqual((pending.kind, pending.qty_out), ('out', 5.0))

    def test_the_item_filter_leaves_other_items_out(self):
        self._move(self.supplier, self.loc_a, 1.0, product=self.other)
        self._move(self.supplier, self.loc_a, 2.0)
        rows, _action = self._rows(show_opening=False)
        self.assertEqual(rows.product_id, self.product)

    def test_a_warehouse_covers_every_location_inside_it(self):
        self._move(self.supplier, self.loc_a, 4.0)
        warehouse = self.env.ref('stock.warehouse0')
        rows, _action = self._rows(location_ids=[(5,)],
                                   warehouse_ids=[(6, 0, warehouse.ids)], show_opening=False)
        self.assertIn(self.loc_a, rows.location_id)

    def test_show_opens_the_grouped_list(self):
        self._move(self.supplier, self.loc_a, 1.0)
        _rows, action = self._rows(group_by='product', group_by_2='location')
        self.assertEqual(action['res_model'], 'ksw.stock.movement.line')
        self.assertEqual(action['context']['group_by'], ['product_id', 'location_id'])

    def test_dates_the_wrong_way_round_are_refused(self):
        wizard = self.env['ksw.stock.movement.wizard'].create({
            'date_from': fields.Date.today(), 'date_to': fields.Date.today() - timedelta(days=1)})
        with self.assertRaises(UserError):
            wizard.action_show()

    def test_no_opening_row_unless_asked(self):
        self._move(self.supplier, self.loc_a, 7.0, days_ago=40)
        self._move(self.loc_a, self.customer, 2.0)
        rows, _action = self._rows()
        self.assertFalse(rows.filtered(lambda r: r.kind == 'opening'))
        self.assertEqual(rows.kind, 'out')

    def test_a_sales_delivery_carries_its_price_and_vat(self):
        if 'sale_line_id' not in self.env['stock.move']._fields:
            self.skipTest('Sales is not installed')
        tax = self.env['account.tax'].create({
            'name': 'KSW SM VAT 15', 'amount': 15.0, 'amount_type': 'percent',
            'type_tax_use': 'sale'})
        partner = self.env['res.partner'].create({'name': 'KSW SM Customer'})
        order = self.env['sale.order'].create({
            'partner_id': partner.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'product_uom_qty': 4.0,
                'price_unit': 12.5, 'discount': 20.0, 'tax_ids': [(6, 0, tax.ids)]})],
        })
        order.action_confirm()
        picking = order.picking_ids
        self._move(self.supplier, picking.location_id, 4.0)
        picking.move_ids.write({'quantity': 4.0, 'picked': True})
        picking.button_validate()
        rows, _action = self._rows(location_ids=[(6, 0, picking.location_id.ids)])
        row = rows.filtered(lambda r: r.kind == 'out' and r.picking_id == picking)
        self.assertEqual(row.price_unit, 10.0, '12.5 less 20 %')
        self.assertEqual(row.amount_untaxed, 40.0)
        self.assertEqual(row.amount_tax, 6.0)
        self.assertEqual(row.amount_total, 46.0)

    def test_a_transfer_has_no_price(self):
        self._move(self.supplier, self.loc_a, 3.0)
        rows, _action = self._rows()
        self.assertFalse(rows.price_unit)
        self.assertFalse(rows.amount_untaxed)
