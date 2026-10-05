from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import Form, tagged

from .test_water_delivery import STUB_SIGNATURE, WaterDeliveryCommon


@tagged('post_install', '-at_install')
class TestMonthEndInvoice(WaterDeliveryCommon):
    """A client's delivered notes for a period become one invoice, with a
    section per product and one line per note underneath."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.billing_user = cls.env['res.users'].create({
            'name': 'Billing Clerk', 'login': 'billing.clerk@ksw.test',
            'group_ids': [(6, 0, [
                cls.env.ref('KSW_water_delivery.group_water_billing').id,
                cls.env.ref('account.group_account_invoice').id,
            ])],
        })
        cls.billing_only_user = cls.env['res.users'].create({
            'name': 'Billing Without Accounting', 'login': 'billing.only@ksw.test',
            'group_ids': [(6, 0, [
                cls.env.ref('KSW_water_delivery.group_water_billing').id,
            ])],
        })
        cls.today = fields.Date.context_today(cls.env['stock.picking'])

    # --- helpers --------------------------------------------------------------
    def _note(self, **extra):
        return self._issue_note(**extra).picking_id.sudo()

    def _wizard(self, partner, user=None, date_from=None, date_to=None, **ctx):
        Wizard = self.env['ksw.water.invoice.wizard'].with_user(user or self.billing_user)
        form = Form(Wizard.with_context(**ctx))
        form.partner_id = partner
        form.date_from = date_from or self.today
        form.date_to = date_to or self.today
        return form

    def _invoice(self, partner, **kw):
        wizard = self._wizard(partner, **kw).save()
        action = wizard.action_create_invoice()
        return self.env['account.move'].browse(action['res_id'])

    # --- the invoice ------------------------------------------------------------
    def test_two_products_become_two_sections_one_line_per_note(self):
        a1 = self._note(partner_id=self.multi_customer.id, product_id=self.water.id,
                        entered_qty=10.0)
        a2 = self._note(partner_id=self.multi_customer.id, product_id=self.water.id,
                        entered_qty=6.0)
        b1 = self._note(partner_id=self.multi_customer.id,
                        product_id=self.water_other.id, entered_qty=4.0)

        form = self._wizard(self.multi_customer)
        self.assertEqual(len(form.picking_ids), 3)
        self.assertEqual(form.note_count, 3)
        wizard = form.save()
        by_product = {line.product_id: line for line in wizard.line_ids}
        self.assertEqual(by_product[self.water].quantity, 16.0)
        self.assertEqual(by_product[self.water].note_count, 2)
        self.assertAlmostEqual(by_product[self.water].amount_untaxed, 16 * 150.0)
        self.assertEqual(by_product[self.water_other].quantity, 4.0)
        self.assertAlmostEqual(form.amount_total, (16 * 150.0 + 4 * 90.0) * 1.15)

        invoice = wizard.action_create_invoice()
        invoice = self.env['account.move'].browse(invoice['res_id'])
        self.assertEqual(invoice.state, 'draft')
        self.assertEqual(invoice.partner_id, self.multi_customer)
        self.assertAlmostEqual(invoice.amount_total, (16 * 150.0 + 4 * 90.0) * 1.15)

        sections = invoice.invoice_line_ids.filtered(
            lambda l: l.display_type == 'line_section')
        products = invoice.invoice_line_ids.filtered(
            lambda l: l.display_type == 'product')
        self.assertEqual(len(sections), 2)
        self.assertEqual(len(products), 3, 'one invoice line per delivery note')
        # Each note's line is linked to that note's own order line -- the
        # property every native consequence hangs off.
        for note in (a1, a2, b1):
            line = products.filtered(lambda l: note.name in l.name)
            self.assertEqual(line.sale_line_ids, note.sale_id.order_line)
            self.assertEqual(note.x_invoice_id, invoice)
            self.assertEqual(note.x_invoice_status, 'invoiced')
        # Lines sit under their own product's section.
        for line in products:
            self.assertEqual(line.parent_id.display_type, 'line_section')
            self.assertIn(line.product_id.display_name, line.parent_id.name)

    def test_the_client_terms_and_delivery_date_are_carried(self):
        note = self._note(entered_qty=2.0)
        invoice = self._invoice(self.customer)
        self.assertEqual(invoice.invoice_payment_term_id,
                         self.env.ref('KSW_water_delivery.account_payment_term_60days'))
        self.assertEqual(invoice.delivery_date, note.x_delivery_date)
        self.assertEqual(invoice.invoice_date, self.today)

    def test_a_rate_change_inside_the_period_is_its_own_section(self):
        self._note(entered_qty=1.0)
        self.rate.price = 220.0
        self._note(entered_qty=1.0)
        invoice = self._invoice(self.customer)
        sections = invoice.invoice_line_ids.filtered(
            lambda l: l.display_type == 'line_section')
        self.assertEqual(len(sections), 2)
        self.assertAlmostEqual(invoice.amount_untaxed, 420.0)

    # --- summary by default, detail on request --------------------------------
    def test_summary_is_the_default(self):
        self._note(entered_qty=2.0)
        self._note(entered_qty=3.0)
        invoice = self._invoice(self.customer)
        self.assertTrue(invoice.x_water_summary)
        # Printed: one row for the product, nothing per note.
        self.assertFalse(invoice._get_move_lines_to_report().filtered(
            lambda l: l.x_water_section or l.parent_id.x_water_section))
        [row] = invoice._water_summary_rows()
        self.assertEqual(row['quantity'], '5')
        self.assertEqual(row['price_unit'], 200.0)
        self.assertAlmostEqual(row['price_subtotal'], invoice.amount_untaxed)
        # The notes are still on the invoice, one line each.
        self.assertEqual(len(invoice.invoice_line_ids.filtered(
            lambda l: l.display_type == 'product')), 2)

    def test_ticking_show_notes_prints_every_note(self):
        a, b = self._note(), self._note()
        form = self._wizard(self.customer)
        form.show_notes = True
        invoice = self.env['account.move'].browse(form.save().action_create_invoice()['res_id'])
        self.assertFalse(invoice.x_water_summary)
        printed = invoice._get_move_lines_to_report().mapped('name')
        self.assertTrue(any(a.name in n for n in printed))
        self.assertTrue(any(b.name in n for n in printed))

    def test_the_printed_invoice_follows_the_choice(self):
        note = self._note()
        invoice = self._invoice(self.customer)
        render = lambda: self.env['ir.actions.report']._render_qweb_html(
            'account.account_invoices', invoice.ids)[0].decode()
        html = render()
        self.assertIn('water_summary_name', html)
        self.assertNotIn(note.name, html)
        invoice.x_water_summary = False
        html = render()
        self.assertNotIn('water_summary_name', html)
        self.assertIn(note.name, html)

    # --- printed in another unit ----------------------------------------------
    def _trip(self, size):
        return self.env['uom.uom'].search([
            ('name', '=', 'Trip (%d m³)' % size), ('relative_uom_id', '=', self.uom_m3.id)])

    def test_m3_can_be_printed_in_trips(self):
        trip = self._trip(32)
        self.assertTrue(trip, 'the module creates Trip (32 m³) on install')
        self._note(entered_qty=32.0)
        self._note(entered_qty=32.0)
        form = self._wizard(self.customer)
        with form.line_ids.edit(0) as row:
            row.display_uom_id = trip
            self.assertEqual(row.display_quantity, '2 %s' % trip.name)
            self.assertEqual(row.display_price, 6400.0)
        invoice = self.env['account.move'].browse(form.save().action_create_invoice()['res_id'])
        [row] = invoice._water_summary_rows()
        self.assertEqual((row['quantity'], row['uom'], row['price_unit']), ('2', trip.display_name, 6400.0))
        self.assertAlmostEqual(row['price_subtotal'], 12800.0)
        # The money did not go through the conversion: the lines are still m³.
        lines = invoice.invoice_line_ids.filtered(lambda l: l.display_type == 'product')
        self.assertEqual(set(lines.mapped('product_uom_id')), {self.uom_m3})
        self.assertEqual(set(lines.mapped('quantity')), {32.0})

    def test_a_part_load_is_an_exact_fraction_of_a_trip(self):
        trip = self._trip(32)
        part = self._note(entered_qty=30.0)
        full = self._note(entered_qty=32.0)
        invoice = (part | full)._water_create_invoice(display_uoms={self.water.id: trip})
        [row] = invoice._water_summary_rows()
        self.assertEqual(row['quantity'], '1.9375')
        self.assertAlmostEqual(1.9375 * row['price_unit'], row['price_subtotal'])
        self.assertAlmostEqual(row['price_subtotal'], 62 * 200.0)

    def test_only_units_of_the_same_kind_are_offered(self):
        self._note()
        form = self._wizard(self.customer)
        with form.line_ids.edit(0) as row:
            allowed = row.allowed_uom_ids
        self.assertIn(self._trip(32), allowed)
        self.assertNotIn(self.env.ref('uom.product_uom_unit'), allowed)
        note = self._wizard(self.customer).picking_ids[0]
        with self.assertRaises(UserError):
            self.env['stock.picking'].browse(note.id)._water_create_invoice(
                display_uoms={self.water.id: self.env.ref('uom.product_uom_unit')})

    def test_the_chosen_unit_survives_removing_a_note(self):
        trip = self._trip(32)
        self._note(entered_qty=32.0)
        removed = self._note(entered_qty=32.0)
        form = self._wizard(self.customer)
        with form.line_ids.edit(0) as row:
            row.display_uom_id = trip
        form.picking_ids.remove(id=removed.id)
        wizard = form.save()
        self.assertEqual(wizard.line_ids.display_uom_id, trip)
        self.assertEqual(wizard.line_ids.note_count, 1)

    # --- billed once, and only once ------------------------------------------
    def test_invoiced_notes_are_not_offered_again(self):
        self._note()
        self._invoice(self.customer)
        form = self._wizard(self.customer)
        self.assertFalse(form.picking_ids)
        with self.assertRaises(UserError):
            self.env['ksw.water.invoice.wizard'].with_user(self.billing_user).create({
                'partner_id': self.customer.id,
                'date_from': self.today, 'date_to': self.today,
            }).action_create_invoice()

    def test_the_same_notes_cannot_be_invoiced_twice_directly(self):
        note = self._note()
        note._water_create_invoice()
        with self.assertRaises(UserError):
            note._water_create_invoice()

    def test_cancelling_the_draft_frees_the_notes(self):
        note = self._note()
        invoice = self._invoice(self.customer)
        invoice.sudo().button_cancel()
        self.assertEqual(note.x_invoice_status, 'to invoice')
        self.assertFalse(note.x_invoice_id)
        self.assertEqual(len(self._wizard(self.customer).picking_ids), 1)

    def test_a_credit_note_for_one_note_reopens_only_that_note(self):
        kept = self._note(entered_qty=1.0)
        disputed = self._note(entered_qty=3.0)
        invoice = self._invoice(self.customer).sudo()
        invoice.action_post()

        disputed_line = invoice.invoice_line_ids.filtered(lambda l: disputed.name in l.name)
        refund = invoice._reverse_moves()
        # Credit only the disputed note: drop the other line from the refund.
        refund.invoice_line_ids.filtered(
            lambda l: l.display_type == 'product'
            and l.sale_line_ids != disputed_line.sale_line_ids).unlink()
        refund.action_post()

        self.assertEqual(disputed.x_invoice_status, 'to invoice')
        self.assertEqual(kept.x_invoice_status, 'invoiced')

    # --- what is left out, and why --------------------------------------------
    def test_only_the_period_is_collected(self):
        inside = self._note()
        outside = self._note()
        outside.date_done = fields.Datetime.now() - timedelta(days=40)
        form = self._wizard(self.customer)
        self.assertEqual(form.picking_ids.ids, inside.ids)

    def test_an_unsigned_note_is_left_out_and_named(self):
        signed = self._note()
        unsigned = self._note()
        unsigned.write({'signature': False})
        form = self._wizard(self.customer)
        self.assertEqual(form.picking_ids.ids, signed.ids)
        self.assertIn(unsigned.name, form.excluded_summary)

    def test_a_note_issued_without_signing_in_the_pilot_is_billable(self):
        self.picking_type.x_signature_required = False
        note = self._note(signature=False)
        self.assertEqual(self._wizard(self.customer).picking_ids.ids, note.ids)

    def test_a_held_note_is_left_out_and_named(self):
        held = self.env['stock.picking'].sudo()._issue_water_note({
            'partner_id': self.customer.id, 'product_id': self.water.id,
            'entered_qty': 1.0, 'entered_uom': 'product',
            'driver_id': self.driver.id, 'vehicle_id': self.vehicle.id,
            'signature': STUB_SIGNATURE, 'signed_by': 'X',
            'captured_at': fields.Datetime.now(), 'issued_offline': True,
            'hold_reasons': ['Test hold'],
        }, location_mode='hold')
        self.assertNotEqual(held.state, 'done')
        form = self._wizard(self.customer)
        self.assertNotIn(held.id, form.picking_ids.ids)
        self.assertIn(held.name, form.excluded_summary)

    def test_a_note_taken_off_stays_waiting(self):
        kept = self._note()
        removed = self._note()
        form = self._wizard(self.customer)
        form.picking_ids.remove(id=removed.id)
        self.assertIn(removed.name, form.excluded_summary)
        invoice = self.env['account.move'].browse(
            form.save().action_create_invoice()['res_id'])
        self.assertEqual(kept.x_invoice_id, invoice)
        self.assertEqual(removed.x_invoice_status, 'to invoice')

    # --- started from a selection on the notes list ---------------------------
    def test_a_selection_is_the_scope(self):
        picked = self._note()
        self._note()
        form = self._wizard(self.customer, active_model='stock.picking',
                            active_ids=picked.ids)
        self.assertEqual(form.picking_ids.ids, picked.ids)

    def test_a_selection_across_two_clients_is_refused(self):
        a = self._note()
        b = self._note(partner_id=self.multi_customer.id, product_id=self.water.id)
        with self.assertRaises(UserError):
            self.env['ksw.water.invoice.wizard'].with_user(self.billing_user).with_context(
                active_model='stock.picking', active_ids=(a | b).ids,
            ).default_get(['partner_id'])

    # --- who may do it --------------------------------------------------------
    def test_billing_without_the_invoicing_right_is_refused(self):
        self._note()
        wizard = self._wizard(self.customer, user=self.billing_only_user).save()
        with self.assertRaises(UserError):
            wizard.action_create_invoice()

    def test_a_driver_is_refused(self):
        self._note()
        wizard = self.env['ksw.water.invoice.wizard'].sudo().create({
            'partner_id': self.customer.id,
            'date_from': self.today, 'date_to': self.today,
        })
        with self.assertRaises(UserError):
            wizard.with_user(self.driver_user).action_create_invoice()

    # --- revenue account: branch x product, the way BAS posts it ------------
    def test_the_line_posts_to_its_branch_revenue_account(self):
        branch_account = self.env['account.account'].create({
            'name': 'Sales, branch 172 (test)', 'code': '4999172',
            'account_type': 'income',
        })
        other_account = self.env['account.account'].create({
            'name': 'Sales, branch 999 (test)', 'code': '4999999',
            'account_type': 'income',
        })
        Revenue = self.env['ksw.water.revenue.account']
        Revenue.create({'branch_code': '172', 'product_id': self.water.id,
                        'account_id': branch_account.id})
        Revenue.create({'branch_code': '999', 'product_id': self.water.id,
                        'account_id': other_account.id})
        self._note()
        invoice = self._invoice(self.customer)
        line = invoice.invoice_line_ids.filtered(lambda l: l.display_type == 'product')
        self.assertEqual(line.account_id, branch_account)

    def test_no_revenue_row_falls_back_to_the_product_account(self):
        self._note()
        invoice = self._invoice(self.customer)
        line = invoice.invoice_line_ids.filtered(lambda l: l.display_type == 'product')
        self.assertEqual(line.account_id, self.water.property_account_income_id)

    def test_editing_an_imported_row_takes_it_over(self):
        account = self.env['account.account'].create({
            'name': 'Sales (test)', 'code': '4999001', 'account_type': 'income'})
        row = self.env['ksw.water.revenue.account'].create({
            'branch_code': '172', 'product_id': self.water.id, 'source': 'bas'})
        row.account_id = account
        self.assertEqual(row.source, 'manual')

    def test_a_four_decimal_rate_shows_the_exact_trip_price(self):
        self.rate.price = 16.5625
        self.assertEqual(self.rate.price, 16.5625, 'the register keeps BAS\'s four decimals')
        self._note(entered_qty=32.0)
        form = self._wizard(self.customer)
        with form.line_ids.edit(0) as row:
            row.display_uom_id = self._trip(32)
            self.assertEqual(row.display_price, 530.0)
