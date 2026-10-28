"""Water prices come from the client's own pricelist (Oct 2026).

The module used to keep its own rate register next to Odoo's pricelists, and
a customer whose price was set on a pricelist in the Sales app did not exist
for the drivers at all. These pin the single source of truth.
"""
from odoo.exceptions import UserError
from odoo.tests import tagged

from .test_water_delivery import WaterDeliveryCommon


@tagged('post_install', '-at_install')
class TestClientPricelist(WaterDeliveryCommon):

    def _pricelist(self, partner):
        return partner.with_company(self.company).specific_property_product_pricelist

    def test_a_client_rate_lives_on_the_clients_own_pricelist(self):
        pricelist = self._pricelist(self.customer)
        self.assertTrue(pricelist)
        self.assertEqual(pricelist.x_client_id, self.customer)
        self.assertEqual(self.rate.pricelist_id, pricelist)

    def test_two_clients_never_share_a_pricelist(self):
        """Same price, still separate: re-pricing one must not move the other."""
        twin = self.env['res.partner'].create({'name': 'Twin Client', 'customer_rank': 1})
        self.env['product.pricelist.item'].create({
            'x_client_id': twin.id, 'product_id': self.water.id, 'fixed_price': 200.0})
        self.assertNotEqual(self._pricelist(twin), self._pricelist(self.customer))

    def test_a_pricelist_made_in_sales_puts_the_client_on_the_list(self):
        """The case that started this: a price set the ordinary Odoo way, with
        nothing entered in Client Rates."""
        client = self.env['res.partner'].create({'name': 'Sales-Priced Client', 'customer_rank': 1})
        pricelist = self.env['product.pricelist'].create({
            'name': 'Sales-Priced Client',
            'item_ids': [(0, 0, {
                'applied_on': '1_product', 'product_tmpl_id': self.water.product_tmpl_id.id,
                'compute_price': 'fixed', 'fixed_price': 4.6875,
            })],
        })
        client.specific_property_product_pricelist = pricelist

        Pricing = self.env['ksw.water.pricing']
        self.assertIn(client.id, Pricing._rated_partner_ids())
        self.assertEqual(Pricing._products_for_partner(client), self.water)
        picking = self._issue_note(partner_id=client.id, entered_qty=32.0).picking_id.sudo()
        self.assertEqual(picking.sale_id.order_line.price_unit, 4.6875)
        self.assertEqual(picking.x_amount_untaxed, 150.0)

    def test_the_default_pricelist_does_not_open_every_customer(self):
        """Every contact without an assigned pricelist gets the company
        default; a fixed rule there must not put all of them on the list."""
        default = self.env['product.pricelist'].create({
            'name': 'Everybody', 'sequence': 1,
            'item_ids': [(0, 0, {
                'applied_on': '0_product_variant', 'product_id': self.water.id,
                'compute_price': 'fixed', 'fixed_price': 1.0,
            })],
        })
        self.assertTrue(default)
        self.assertNotIn(self.unrated_customer.id,
                         self.env['ksw.water.pricing']._rated_partner_ids())
        with self.assertRaises(UserError):
            self._issue_note(partner_id=self.unrated_customer.id)

    def test_the_note_and_a_sales_order_agree(self):
        self.rate.fixed_price = 215.0
        picking = self._issue_note(entered_qty=1.0).picking_id.sudo()
        self.assertEqual(picking.sale_id.order_line.price_unit, 215.0)

        self.env.ref('base.group_user').write({'implied_ids': [
            (4, self.env.ref('product.group_product_pricelist').id)]})
        order = self.env['sale.order'].create({
            'partner_id': self.customer.id,
            'order_line': [(0, 0, {'product_id': self.water.id, 'product_uom_qty': 1.0})],
        })
        self.assertEqual(order.pricelist_id, self._pricelist(self.customer))
        self.assertEqual(order.order_line.price_unit, 215.0)

    def test_a_quantity_break_applies_to_the_note(self):
        self.env['product.pricelist.item'].create({
            'x_client_id': self.customer.id, 'product_id': self.water.id,
            'fixed_price': 180.0, 'min_quantity': 10.0,
        })
        small = self._issue_note(entered_qty=2.0).picking_id.sudo()
        large = self._issue_note(entered_qty=12.0).picking_id.sudo()
        self.assertEqual(small.sale_id.order_line.price_unit, 200.0)
        self.assertEqual(large.sale_id.order_line.price_unit, 180.0)

    def test_a_discount_rule_never_prices_at_list_price(self):
        """Only a fixed price is an agreed rate; anything else is refused
        rather than falling through to the product's list price."""
        self.rate.write({'compute_price': 'percentage', 'percent_price': 10.0})
        self.water.list_price = 999.0
        with self.assertRaises(UserError):
            self._issue_note()

    def test_a_shared_pricelist_is_copied_not_edited(self):
        a = self.env['res.partner'].create({'name': 'Group A', 'customer_rank': 1})
        b = self.env['res.partner'].create({'name': 'Group B', 'customer_rank': 1})
        shared = self.env['product.pricelist'].create({
            'name': 'Group',
            'item_ids': [(0, 0, {
                'applied_on': '0_product_variant', 'product_id': self.water_other.id,
                'compute_price': 'fixed', 'fixed_price': 15.0,
            })],
        })
        (a | b).write({'specific_property_product_pricelist': shared.id})

        self.env['product.pricelist.item'].create({
            'x_client_id': a.id, 'product_id': self.water.id, 'fixed_price': 17.0})

        own = self._pricelist(a)
        self.assertNotEqual(own, shared)
        self.assertEqual(self._pricelist(b), shared)
        self.assertEqual(len(shared.item_ids), 1, 'the shared list is untouched')
        self.assertEqual(
            sorted(own.item_ids.mapped('fixed_price')), [15.0, 17.0],
            'A keeps the price he had on the shared list')

    def test_a_bas_rate_edited_by_hand_becomes_manual(self):
        self.rate.write({'x_water_source': 'bas', 'x_bas_price': 200.0})
        self.assertFalse(self.rate.x_differs_from_bas)
        self.rate.fixed_price = 210.0
        self.assertEqual(self.rate.x_water_source, 'manual')
        self.assertTrue(self.rate.x_differs_from_bas)
