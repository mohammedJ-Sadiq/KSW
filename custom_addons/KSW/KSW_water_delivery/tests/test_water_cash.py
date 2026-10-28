"""Cash sales at the branch desk (Oct 2026).

The dispatcher takes the money when the order is placed, the driver delivers
it like any dispatch, and the sale is invoiced and settled the moment the note
is done. What these guard:

1. Dispatch posts the cash into the branch till, for the pricelist price.
2. Delivery invoices at once, in the cash sales journal, already paid, so
   month-end billing never picks it up again.
3. Back to draft or cancelled before delivery hands the cash back.
4. Cash works on any branch, and the cash customer has no site to enforce.
5. A dispatcher, with no accounting rights, can use the form.
"""
from odoo.exceptions import UserError
from odoo.tests import Form, tagged

from .test_offline_capture import OfflineCaptureCommon
from .test_water_delivery import FAR


@tagged('post_install', '-at_install')
class TestWaterCash(OfflineCaptureCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.dispatch_user = cls.env['res.users'].create({
            'name': 'Cash Desk', 'login': 'cashdesk@ksw.test',
            'group_ids': [(6, 0, [cls.env.ref('KSW_water_delivery.group_water_dispatch').id])],
        })
        cls.till = cls.env['account.journal'].create({
            'name': 'Test Branch Till', 'code': 'TTIL', 'type': 'cash'})
        # Like the real tills (C00xx): a receipt posts straight to the till's
        # own account. Left empty it falls back to the archived 101003.
        (cls.till.inbound_payment_method_line_ids | cls.till.outbound_payment_method_line_ids) \
            .write({'payment_account_id': cls.till.default_account_id.id})
        cls.cash_sales = cls.env['account.journal'].create({
            'name': 'Test Cash Sales', 'code': 'TCSL', 'type': 'sale'})
        # Out of ZATCA, which refuses to post until the company is onboarded
        # (dev is not); the ZATCA-blocked path has its own test below.
        cls.cash_sales.edi_format_ids = [(5, 0, 0)]
        cls.picking_type.write({
            'x_issue_mode': 'dispatch',
            'x_dispatcher_user_ids': [(6, 0, cls.dispatch_user.ids)],
            'x_cash_journal_id': cls.till.id,
            'x_cash_sale_journal_id': cls.cash_sales.id,
        })
        cls.picking_type.action_create_cash_customer()
        cls.cash_customer = cls.picking_type.x_cash_partner_id
        cls.env['product.pricelist.item'].create({
            'x_client_id': cls.cash_customer.id, 'product_id': cls.water.id,
            'fixed_price': 10.0})
        cls.Dispatch = cls.env['ksw.water.dispatch']

    def _cash_dispatch(self, **extra):
        values = {
            'picking_type_id': self.picking_type.id, 'x_is_cash': True,
            'x_collected_by': 'ours',
            'partner_id': self.cash_customer.id, 'product_id': self.water.id,
            'entered_qty': 3.0, 'driver_user_id': self.driver_user.id,
            'x_buyer_name': 'Abu Fahad', 'x_buyer_phone': '0500000000',
        }
        values.update(extra)
        dispatch = self.Dispatch.with_user(self.dispatch_user).create(values)
        dispatch.action_dispatch()
        return dispatch

    def _confirm(self, dispatch, **extra):
        payload = self._payload(dispatch_id=dispatch.id, **extra)
        return self.Capture.with_user(self.driver_user)._receive(payload, self.driver)

    # ------------------------------------------------------------------
    def test_the_branch_gets_a_cash_customer_with_its_own_pricelist(self):
        self.assertTrue(self.cash_customer)
        pricelist = self.cash_customer.specific_property_product_pricelist
        self.assertEqual(pricelist.x_client_id, self.cash_customer)

    def test_dispatch_takes_the_cash_into_the_till(self):
        dispatch = self._cash_dispatch()
        self.assertEqual(dispatch.x_cash_received, 34.5, '3 x 10 + 15% VAT')
        payment = dispatch.sudo().x_cash_payment_id
        self.assertEqual(payment.journal_id, self.till)
        self.assertEqual(payment.amount, 34.5)
        self.assertNotEqual(payment.state, 'draft')
        self.assertIn('Abu Fahad', payment.memo)

    def test_delivery_invoices_at_once_and_settles_it(self):
        dispatch = self._cash_dispatch()
        capture = self._confirm(dispatch)
        self.assertEqual(capture.state, 'issued')
        invoice = dispatch.sudo().x_cash_invoice_id
        self.assertEqual(invoice.state, 'posted')
        self.assertEqual(invoice.journal_id, self.cash_sales)
        self.assertEqual(invoice.amount_total, 34.5)
        self.assertIn(invoice.payment_state, ('paid', 'in_payment'))
        self.assertEqual(dispatch.x_cash_invoice_ref, invoice.name)
        self.assertIn('Abu Fahad', invoice.ref)
        # Month-end billing leaves it alone.
        note = dispatch.sudo().picking_id
        self.assertNotIn(note, note.search(note._water_invoiceable_domain()))

    def test_cancel_hands_the_cash_back(self):
        dispatch = self._cash_dispatch()
        payment = dispatch.sudo().x_cash_payment_id
        dispatch.with_user(self.dispatch_user).action_cancel()
        self.assertEqual(dispatch.x_cash_received, 0.0)
        self.assertIn(payment.state, ('canceled', 'cancel'))

    def test_back_to_draft_hands_it_back_and_redispatch_takes_it_again(self):
        dispatch = self._cash_dispatch()
        dispatch.with_user(self.dispatch_user).action_draft()
        self.assertEqual(dispatch.x_cash_received, 0.0)
        dispatch.with_user(self.dispatch_user).write({'entered_qty': 2.0})
        dispatch.with_user(self.dispatch_user).action_dispatch()
        self.assertEqual(dispatch.x_cash_received, 23.0)

    def test_cash_works_on_a_branch_where_drivers_issue(self):
        self.picking_type.x_issue_mode = 'driver'
        dispatch = self._cash_dispatch()
        self.assertEqual(dispatch.state, 'dispatched')

    def test_no_till_no_cash_sale(self):
        self.picking_type.x_cash_journal_id = False
        with self.assertRaises(UserError):
            self._cash_dispatch()

    def test_the_cash_customer_has_no_site_to_enforce(self):
        self.picking_type.x_location_rule = 'enforce'
        dispatch = self._cash_dispatch()
        capture = self._confirm(dispatch, gps_latitude=FAR[0], gps_longitude=FAR[1])
        self.assertEqual(capture.state, 'issued')
        self.assertFalse(capture.picking_id.x_location_exception)

    def test_the_dispatcher_fills_it_in_without_accounting_rights(self):
        self.env.invalidate_all()
        with Form(self.Dispatch.with_user(self.dispatch_user)) as form:
            form.x_is_cash = True
            form.x_collected_by = 'ours'
            self.assertEqual(form.partner_id, self.cash_customer)
            self.assertEqual(form.product_id, self.water)
            form.entered_qty = 2.0
            self.assertEqual(form.x_cash_amount, 23.0)
            form.driver_user_id = self.driver_user
        dispatch = form.record
        dispatch.action_dispatch()
        dispatch.with_user(self.dispatch_user).web_read(
            {'x_cash_received': {}, 'x_cash_invoice_ref': {}, 'x_is_cash': {}})

    def test_a_zatca_blocked_invoice_waits_in_draft_then_settles_on_posting(self):
        zatca = self.env['account.journal'].create({
            'name': 'Test ZATCA Sales', 'code': 'TZSL', 'type': 'sale'})
        self.picking_type.x_cash_sale_journal_id = zatca
        dispatch = self._cash_dispatch()
        self._confirm(dispatch)
        invoice = dispatch.sudo().x_cash_invoice_id
        if zatca.edi_format_ids:
            self.assertEqual(invoice.state, 'draft', 'ZATCA not onboarded: left in draft')
            self.assertEqual(dispatch.x_cash_invoice_ref, 'Draft invoice')
            self.assertTrue(dispatch.message_ids.filtered(
                lambda m: 'accounting must post it' in (m.body or '')))
            # Accounting posts it once ZATCA allows (here: moved off ZATCA).
            invoice.journal_id = self.cash_sales
            invoice.action_post()
        self.assertIn(invoice.payment_state, ('paid', 'in_payment'))
        self.assertEqual(dispatch.x_cash_invoice_ref, invoice.name)

    # -- the dispatcher's form, as the browser drives it --------------------
    def _form(self):
        self.env.invalidate_all()
        form = Form(self.Dispatch.with_user(self.dispatch_user))
        form.x_is_cash = True
        return form

    def test_the_cash_client_is_given_not_chosen(self):
        form = self._form()
        self.assertEqual(form.partner_id, self.cash_customer)
        with self.assertRaises(AssertionError):
            form.partner_id = self.customer   # read-only on a cash sale
        self.assertEqual(form.product_id, self.water, 'the one cash product is preselected')

    def test_a_saved_cash_sale_keeps_the_branch_cash_customer(self):
        """Even if a client is sent (an RPC, an old form), a cash sale is the
        branch cash customer's."""
        dispatch = self.Dispatch.with_user(self.dispatch_user).create({
            'picking_type_id': self.picking_type.id, 'x_is_cash': True,
            'partner_id': self.customer.id, 'product_id': self.water.id,
            'driver_user_id': self.driver_user.id})
        self.assertEqual(dispatch.partner_id, self.cash_customer)

    def test_to_collect_follows_the_quantity_and_the_unit(self):
        form = self._form()
        form.x_collected_by = 'ours'
        form.entered_qty = 4.0
        self.assertEqual(form.x_cash_amount, 46.0)
        form.entered_uom = 'trip'
        self.assertEqual(form.x_cash_amount, 0.0)
        self.assertIn('Choose the driver', form.x_cash_problem)
        form.driver_user_id = self.driver_user          # tanker of 32 m3
        self.assertEqual(form.x_cash_amount, 4 * 32 * 10 * 1.15)
        self.assertFalse(form.x_cash_problem)

    def test_a_branch_without_cash_prices_says_so(self):
        self.cash_customer.specific_property_product_pricelist.item_ids.unlink()
        form = self._form()
        self.assertIn('no cash prices', form.x_cash_problem)

    def test_a_branch_not_set_up_says_so(self):
        self.picking_type.x_cash_partner_id = False
        form = self._form()
        self.assertIn('not set up for cash sales', form.x_cash_problem)

    def test_a_till_without_a_receipt_account_says_what_to_set(self):
        self.till.inbound_payment_method_line_ids.payment_account_id = False
        form = self._form()
        self.assertIn('does not say where receipts post', form.x_cash_problem)
        with self.assertRaises(UserError):
            self._cash_dispatch()

    # -- the customer collects with his own tanker --------------------------
    def _pickup(self, **extra):
        values = {
            'picking_type_id': self.picking_type.id, 'x_is_cash': True,
            'partner_id': self.cash_customer.id, 'product_id': self.water.id,
            'entered_qty': 32.0, 'x_pickup_plate': 'ABC 9999', 'x_pickup_driver': 'Saeed',
        }
        values.update(extra)
        dispatch = self.Dispatch.with_user(self.dispatch_user).create(values)
        dispatch.action_dispatch()
        return dispatch

    def test_a_pickup_is_the_default_for_cash(self):
        form = self._form()
        self.assertEqual(form.x_collected_by, 'customer')
        self.assertTrue(form.x_is_pickup)

    def test_a_pickup_is_paid_delivered_and_invoiced_at_once(self):
        dispatch = self._pickup()
        self.assertEqual(dispatch.state, 'delivered')
        self.assertFalse(dispatch.driver_user_id)
        self.assertEqual(dispatch.x_cash_received, 32 * 10 * 1.15)
        note = dispatch.sudo().picking_id
        self.assertEqual(note.state, 'done')
        self.assertTrue(note.x_is_water_delivery)
        self.assertEqual(note.picking_type_id, self.picking_type)
        self.assertEqual(note.location_id, self.picking_type.default_location_src_id)
        self.assertEqual(note.move_ids.quantity, 32.0)
        self.assertEqual(note.x_signed_by, 'Saeed')
        invoice = dispatch.sudo().x_cash_invoice_id
        self.assertEqual(invoice.journal_id, self.cash_sales)
        self.assertIn(invoice.payment_state, ('paid', 'in_payment'))

    def test_a_pickup_needs_no_driver_and_works_on_any_branch(self):
        self.picking_type.x_issue_mode = 'driver'
        dispatch = self._pickup()
        self.assertEqual(dispatch.state, 'delivered')

    def test_a_pickup_cannot_be_in_trips(self):
        with self.assertRaises(UserError):
            self._pickup(entered_uom='trip')
        form = self._form()
        self.assertEqual(form.entered_uom, 'product')

    def test_no_income_account_is_caught_before_the_cash(self):
        self.picking_type.x_branch_code = False
        self.water.property_account_income_id = False
        self.water.categ_id.property_account_income_categ_id = False
        form = self._form()
        self.assertIn('no Sales Account', form.x_cash_problem)
        with self.assertRaises(UserError):
            self._pickup()
        self.assertFalse(self.env['account.payment'].search([('journal_id', '=', self.till.id)]),
                         'no cash taken')

    def test_a_branch_sells_with_its_own_sales_account_and_no_code(self):
        sales = self.env['account.account'].create({
            'name': 'Test Branch Water Sales', 'code': '4101999001', 'account_type': 'income'})
        self.picking_type.write({'x_branch_code': False, 'x_sales_account_id': sales.id})
        self.water.property_account_income_id = False
        self.water.categ_id.property_account_income_categ_id = False
        form = self._form()
        self.assertFalse(form.x_cash_problem)
        dispatch = self._pickup()
        invoice = dispatch.sudo().x_cash_invoice_id
        self.assertEqual(invoice.invoice_line_ids.account_id, sales)

    def test_a_pickup_is_tagged_with_the_branch_cost_centre(self):
        plan = self.env['account.analytic.plan'].create({'name': 'Test Branches CC'})
        branch_cc = self.env['account.analytic.account'].create(
            {'name': 'Test Branch CC', 'plan_id': plan.id})
        self.picking_type.x_analytic_account_id = branch_cc
        dispatch = self._pickup()
        line = dispatch.sudo().x_cash_invoice_id.invoice_line_ids
        self.assertEqual(line.analytic_distribution, {str(branch_cc.id): 100.0})
