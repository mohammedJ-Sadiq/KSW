from odoo.tests.common import TransactionCase


class TestPurchaseRequestFreeTextLine(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        category = cls.env['it.consumable.category'].create({'name': 'Test Toner'})
        cls.toner = cls.env['it.consumable'].create({
            'name': 'Test Toner 12A',
            'category_id': category.id,
        })

    def test_mixed_request_only_receives_consumables(self):
        request = self.env['it.consumable.purchase.request'].create({
            'line_ids': [
                (0, 0, {'consumable_id': self.toner.id, 'quantity': 3, 'unit_cost': 50}),
                (0, 0, {'name': 'Dell Latitude 5540 Laptop', 'quantity': 1, 'unit_cost': 4500}),
            ],
        })
        toner_line, laptop_line = request.line_ids.sorted('id')
        self.assertEqual(toner_line.name, 'Test Toner 12A')
        self.assertFalse(laptop_line.consumable_id)
        self.assertEqual(request.amount_total, 4650)

        request.action_mark_purchased()

        self.assertEqual(request.state, 'purchased')
        self.assertEqual(self.toner.qty_on_hand, 3)
        moves = self.env['it.consumable.move'].search([('note', 'like', request.name)])
        self.assertEqual(moves.consumable_id, self.toner)

    def test_print_report_with_free_text_line(self):
        request = self.env['it.consumable.purchase.request'].create({
            'line_ids': [(0, 0, {'name': 'Dell Latitude 5540 Laptop', 'quantity': 1})],
        })
        html, _ = self.env['ir.actions.report']._render_qweb_html(
            'KSW_helpdesk.report_it_consumable_purchase_request', request.ids)
        self.assertIn(b'Dell Latitude 5540 Laptop', html)
