from datetime import timedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMovementMultiplier(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        stock = cls.env.ref('stock.stock_location_stock')
        cls.loc = cls.env['stock.location'].create(
            {'name': 'KSW Mult Depot', 'usage': 'internal', 'location_id': stock.id})
        cls.supplier = cls.env.ref('stock.stock_location_suppliers')
        cls.customers = cls.env.ref('stock.stock_location_customers')
        cls.product = cls.env['product.product'].create(
            {'name': 'KSW Mult Water', 'type': 'consu', 'is_storable': True})
        # A new customer starts at 1 (KSW_client_multiplier).
        cls.client = cls.env['res.partner'].create({'name': 'Mult Client', 'customer_rank': 1})
        cls.site = cls.env['res.partner'].create(
            {'name': 'Mult Site', 'parent_id': cls.client.id, 'type': 'delivery'})
        cls.today = fields.Date.context_today(cls.env.user)

    def _deliver(self, partner, qty=1.0, days_ago=0):
        move = self.env['stock.move'].create({
            'product_id': self.product.id, 'product_uom': self.product.uom_id.id,
            'product_uom_qty': qty, 'location_id': self.loc.id,
            'location_dest_id': self.customers.id, 'partner_id': partner.id,
        })
        move._action_confirm()
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
        wizard = self.env['ksw.stock.movement.wizard'].create(dict({
            'location_ids': [(6, 0, self.loc.ids)],
            'product_ids': [(6, 0, self.product.ids)],
            'date_from': self.today - timedelta(days=10),
        }, **vals))
        wizard.action_show()
        return wizard.line_ids

    def test_each_row_carries_the_multiplier_of_its_day(self):
        client = self.env['ksw.client.multiplier'].search([('partner_id', '=', self.client.id)])
        self.env['ksw.client.multiplier.rate'].create({
            'client_id': client.id, 'multiplier': 1.5,
            'date_from': self.today - timedelta(days=2), 'source': 'request'})
        self._deliver(self.client, days_ago=5)
        self._deliver(self.client)
        rows = self._rows(show_opening=False).filtered(lambda r: r.kind == 'out').sorted('date')
        self.assertEqual(rows.mapped('x_multiplier'), [1.0, 1.5],
                         'a change applies from its effective date only')
        self.assertEqual(sum(rows.mapped('x_multiplier')), 2.5)

    def test_a_delivery_address_uses_its_companys_multiplier(self):
        self._deliver(self.site)
        row = self._rows(show_opening=False).filtered(lambda r: r.kind == 'out')
        self.assertEqual(row.x_multiplier, 1.0)

    def test_rows_without_a_customer_stay_empty(self):
        self.env['stock.move'].create({
            'product_id': self.product.id, 'product_uom': self.product.uom_id.id,
            'product_uom_qty': 3.0, 'location_id': self.supplier.id,
            'location_dest_id': self.loc.id,
        })._action_confirm()._action_done()
        rows = self._rows()
        self.assertFalse(any(rows.mapped('x_multiplier')))
