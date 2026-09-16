import base64

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import Form, TransactionCase, tagged

# A 1x1 transparent PNG: enough for a Binary field to be non-empty.
STUB_SIGNATURE = base64.b64encode(base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM'
    'IQAAAABJRU5ErkJggg=='
))

# Dhahran-ish, and a point roughly 4 km away.
HERE = (26.4207, 50.0888)
FAR = (26.4567, 50.0888)


@tagged('post_install', '-at_install')
class WaterDeliveryCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.picking_type = cls.env.ref('KSW_water_delivery.picking_type_water_out')
        # The pilot branch ships with signatures OFF for the proof of concept.
        # Most of this suite is about the signed flow, so turn it on here and
        # let TestProofOfConcept turn it back off.
        cls.picking_type.write({
            'x_branch_code': '172',
            'x_signature_required': True,
            'x_location_rule': 'capture',
            'x_location_radius_m': 300,
        })

        cls.tax_15 = cls.env['account.tax'].create({
            'name': 'VAT 15% (test)', 'amount': 15.0, 'amount_type': 'percent',
            'type_tax_use': 'sale', 'company_id': cls.company.id,
        })
        income_account = cls.env['account.account'].search(
            [('account_type', '=', 'income')], limit=1)

        # Resolve m³ the way production does. There are two in this database --
        # core's `uom.product_uom_cubic_meter` is archived, and the one every
        # BAS-imported product actually uses was created by KSW_bas_gl_import
        # and carries no external id.
        cls.uom_m3 = cls.env['uom.uom'].search(
            [('name', '=', 'm³'), ('active', '=', True)], limit=1)

        cls.water = cls._make_product('Test Sweet Water', income_account)
        cls.water_other = cls._make_product('Test Raw Water', income_account)

        cls.customer = cls.env['res.partner'].create({
            'name': 'Test Water Customer', 'customer_rank': 1,
            'property_payment_term_id': cls.env.ref(
                'KSW_water_delivery.account_payment_term_60days').id,
        })
        cls.multi_customer = cls.env['res.partner'].create({
            'name': 'Two Product Customer', 'customer_rank': 1,
        })
        cls.unrated_customer = cls.env['res.partner'].create({
            'name': 'Customer With No Rate', 'customer_rank': 1,
        })

        Rate = cls.env['ksw.water.rate']
        cls.rate = Rate.create({
            'partner_id': cls.customer.id, 'product_id': cls.water.id, 'price': 200.0,
        })
        Rate.create({
            'partner_id': cls.multi_customer.id, 'product_id': cls.water.id, 'price': 150.0,
        })
        Rate.create({
            'partner_id': cls.multi_customer.id, 'product_id': cls.water_other.id,
            'price': 90.0,
        })

        # This database already holds 455 real client/branch pairs imported
        # from BAS, and the pilot branch is 172 — so a test client has to be
        # listed for 172 as well or the picker would rightly exclude it.
        Coverage = cls.env['ksw.water.client.branch']
        Coverage.create([
            {'partner_id': cls.customer.id, 'branch_code': '172'},
            {'partner_id': cls.multi_customer.id, 'branch_code': '172'},
        ])
        # Priced, but served by a different branch.
        cls.other_branch_customer = cls.env['res.partner'].create({
            'name': 'Other Branch Customer', 'customer_rank': 1,
        })
        cls.env['ksw.water.rate'].create({
            'partner_id': cls.other_branch_customer.id,
            'product_id': cls.water.id, 'price': 111.0,
        })
        Coverage.create({
            'partner_id': cls.other_branch_customer.id, 'branch_code': '999',
        })

        cls.driver_user = cls._make_driver('Driver One')
        cls.other_driver_user = cls._make_driver('Driver Two')
        cls.driver = cls.driver_user.employee_id
        cls.other_driver = cls.other_driver_user.employee_id

        cls.vehicle = cls.env['ksw.fleet.vehicle'].create({
            'name': 'T-100', 'client_id': cls.company.partner_id.id,
            'vehicle_type': 'trailer', 'plate_number': 'ABC 1234',
            'driver_id': cls.driver.id, 'state': 'confirmed',
            'x_capacity_m3': 32.0,
            'x_picking_type_id': cls.picking_type.id,
        })

    @classmethod
    def _make_product(cls, name, income_account):
        return cls.env['product.product'].create({
            'name': name, 'type': 'consu', 'is_storable': False,
            'invoice_policy': 'delivery', 'uom_id': cls.uom_m3.id,
            'sale_ok': True, 'list_price': 0.0,
            'taxes_id': [(6, 0, cls.tax_15.ids)],
            'property_account_income_id': income_account.id,
        })

    @classmethod
    def _make_driver(cls, name):
        user = cls.env['res.users'].create({
            'name': name,
            'login': name.lower().replace(' ', '.') + '@ksw.test',
            'group_ids': [(6, 0, [
                cls.env.ref('KSW_water_delivery.group_water_driver').id,
            ])],
        })
        cls.env['hr.employee'].create({'name': name, 'user_id': user.id})
        return user

    def _wizard_vals(self, user, **extra):
        vals = {
            'partner_id': self.customer.id,
            'product_id': self.water.id,
            'entered_qty': 1.0,
            'entered_uom': 'product',
            'signature': STUB_SIGNATURE,
            'signed_by': 'Site Foreman',
            'driver_id': user.sudo().employee_id.id,
            'vehicle_id': self.vehicle.id,
            'gps_latitude': HERE[0], 'gps_longitude': HERE[1], 'gps_accuracy': 10.0,
        }
        vals.update(extra)
        return vals

    def _issue_note(self, user=None, **extra):
        user = user or self.driver_user
        Wizard = self.env['ksw.water.delivery.wizard'].with_user(user)
        wizard = Wizard.create(self._wizard_vals(user, **extra))
        wizard.action_confirm()
        return wizard


@tagged('post_install', '-at_install')
class TestWaterDeliveryIssue(WaterDeliveryCommon):

    def test_issue_note_builds_the_native_chain(self):
        """One tap produces a confirmed sale order and a validated, signed
        delivery note -- the whole point being that month end then needs no
        code of ours at all."""
        picking = self._issue_note(entered_qty=2.0).picking_id.sudo()

        self.assertEqual(picking.state, 'done')
        self.assertTrue(picking.is_signed)
        self.assertEqual(picking.picking_type_id, self.picking_type)
        self.assertTrue(picking.name.startswith('WH/WTR/'))

        order = picking.sale_id
        self.assertEqual(order.state, 'sale')
        self.assertEqual(order.order_line.qty_delivered, 2.0)
        self.assertEqual(order.invoice_status, 'to invoice')

    def test_the_note_is_priced_from_the_agreed_rate(self):
        """Not from the product's list price -- the rate is per client, which
        is the whole reason the register exists."""
        self.water.list_price = 999.0
        picking = self._issue_note(entered_qty=3.0).picking_id.sudo()

        self.assertEqual(picking.sale_id.order_line.price_unit, 200.0)
        self.assertEqual(picking.x_amount_untaxed, 600.0)
        self.assertEqual(picking.x_amount_tax, 90.0)
        self.assertEqual(picking.x_amount_total, 690.0)

    def test_a_client_with_no_rate_cannot_be_delivered_to(self):
        with self.assertRaises(UserError):
            self._issue_note(partner_id=self.unrated_customer.id)

    def test_note_records_who_delivered_and_who_signed(self):
        picking = self._issue_note().picking_id.sudo()

        self.assertEqual(picking.x_driver_id, self.driver)
        self.assertEqual(picking.x_vehicle_id, self.vehicle)
        self.assertEqual(picking.x_signed_by, 'Site Foreman')
        self.assertTrue(picking.x_signed_on)
        self.assertEqual(picking.x_branch_code, '172')

    def test_the_branch_on_a_signed_note_is_history(self):
        picking = self._issue_note().picking_id.sudo()
        self.assertEqual(picking.x_branch_code, '172')

        self.picking_type.x_branch_code = '180'
        picking.invalidate_recordset()
        self.assertEqual(picking.x_branch_code, '172')

    def test_signed_pdf_is_attached_without_anyone_uploading_it(self):
        picking = self._issue_note().picking_id.sudo()
        attachments = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'stock.picking'), ('res_id', '=', picking.id),
            ('name', 'like', 'signed_delivery_slip'),
        ])
        self.assertTrue(attachments)
        self.assertEqual(attachments.mimetype, 'application/pdf')


@tagged('post_install', '-at_install')
class TestWaterDeliveryPickers(WaterDeliveryCommon):
    """What the driver is offered. A picker wider than his authority is the
    failure this module is meant to avoid."""

    def _new_form(self, **vals):
        Wizard = self.env['ksw.water.delivery.wizard'].with_user(self.driver_user)
        defaults = Wizard.default_get(list(Wizard._fields))
        defaults.update(vals)
        return Wizard.new(defaults)

    def test_only_clients_with_a_rate_are_offered(self):
        form = self._new_form(vehicle_id=self.vehicle.id)
        allowed = form.allowed_partner_ids

        self.assertIn(self.customer.id, allowed.ids)
        self.assertIn(self.multi_customer.id, allowed.ids)
        # The negative half is the one that matters.
        self.assertNotIn(self.unrated_customer.id, allowed.ids)

    def test_one_product_is_chosen_for_him_and_locked(self):
        """257 of 312 real clients buy exactly one product. A picker with one
        option in it is a question with one answer.

        Driven through `Form`, not `new()`: preselection happens in an onchange,
        and a plain `new()` record never fires one. Testing it the other way is
        what let the field arrive empty at the database."""
        form = Form(self.env['ksw.water.delivery.wizard'].with_user(self.driver_user))
        form.vehicle_id = self.vehicle
        form.partner_id = self.customer

        self.assertTrue(form.product_locked)
        self.assertEqual(form.product_id, self.water)

    def test_several_products_are_offered_as_a_choice(self):
        form = self._new_form(vehicle_id=self.vehicle.id)
        form.partner_id = self.multi_customer

        self.assertEqual(len(form.allowed_product_ids), 2)
        self.assertFalse(form.product_locked)

    def test_the_agreed_rate_is_shown_before_he_commits(self):
        """This failed silently once: on an unsaved form a relational value is
        NewId-wrapped, a search on it matches nothing and raises nothing, and
        the preview just came back blank."""
        form = Form(self.env['ksw.water.delivery.wizard'].with_user(self.driver_user))
        form.vehicle_id = self.vehicle
        form.partner_id = self.customer

        self.assertTrue(form.rate_preview, 'the rate must survive the onchange')
        self.assertIn('200', form.rate_preview)


@tagged('post_install', '-at_install')
class TestWaterDeliveryTrips(WaterDeliveryCommon):
    """A trip is not a unit of measure: a trailer carries 32 m³ and an Isuzu far
    less, so the factor belongs to the truck."""

    def test_trips_convert_by_the_tankers_capacity(self):
        picking = self._issue_note(entered_qty=2.0, entered_uom='trip').picking_id.sudo()

        self.assertEqual(picking.x_load_qty, 64.0)
        self.assertEqual(picking.sale_id.order_line.product_uom_qty, 64.0)
        self.assertEqual(picking.x_amount_untaxed, 64.0 * 200.0)

    def test_what_he_typed_is_kept_next_to_what_it_became(self):
        picking = self._issue_note(entered_qty=2.0, entered_uom='trip').picking_id.sudo()

        self.assertEqual(picking.x_entered_qty, 2.0)
        self.assertEqual(picking.x_entered_uom, 'trip')
        self.assertEqual(picking.x_load_qty, 64.0)

    def test_a_smaller_tanker_gives_a_smaller_trip(self):
        isuzu = self.env['ksw.fleet.vehicle'].create({
            'name': 'IS-900', 'client_id': self.company.partner_id.id,
            'vehicle_type': 'isuzu', 'driver_id': self.driver.id,
            'state': 'confirmed', 'x_capacity_m3': 6.0,
            'x_picking_type_id': self.picking_type.id,
        })
        picking = self._issue_note(
            entered_qty=1.0, entered_uom='trip', vehicle_id=isuzu.id,
        ).picking_id.sudo()
        self.assertEqual(picking.x_load_qty, 6.0)

    def test_trips_on_a_tanker_with_no_capacity_are_refused(self):
        self.vehicle.x_capacity_m3 = 0.0
        with self.assertRaises(UserError):
            self._issue_note(entered_qty=1.0, entered_uom='trip')


@tagged('post_install', '-at_install')
class TestWaterDeliveryLocation(WaterDeliveryCommon):

    def test_where_the_driver_was_is_recorded(self):
        picking = self._issue_note().picking_id.sudo()

        self.assertAlmostEqual(picking.x_gps_latitude, HERE[0], places=4)
        self.assertAlmostEqual(picking.x_gps_longitude, HERE[1], places=4)
        self.assertEqual(picking.x_gps_accuracy, 10.0)

    def test_distance_is_measured_once_the_client_is_located(self):
        self.customer.write({
            'partner_latitude': HERE[0], 'partner_longitude': HERE[1],
        })
        picking = self._issue_note().picking_id.sudo()
        self.assertLess(picking.x_gps_distance_m, 50)

    def test_enforcing_refuses_a_note_issued_far_from_the_client(self):
        self.picking_type.x_location_rule = 'enforce'
        self.customer.write({
            'partner_latitude': HERE[0], 'partner_longitude': HERE[1],
        })
        with self.assertRaises(UserError):
            self._issue_note(gps_latitude=FAR[0], gps_longitude=FAR[1])

    def test_enforcing_allows_a_note_issued_at_the_client(self):
        self.picking_type.x_location_rule = 'enforce'
        self.customer.write({
            'partner_latitude': HERE[0], 'partner_longitude': HERE[1],
        })
        picking = self._issue_note().picking_id.sudo()
        self.assertEqual(picking.state, 'done')

    def test_enforcing_still_allows_a_client_nobody_has_located(self):
        """Otherwise turning the rule on locks out every site at once -- and
        the sites are located BY issuing notes at them."""
        self.picking_type.x_location_rule = 'enforce'
        self.assertFalse(self.customer.partner_latitude)

        picking = self._issue_note().picking_id.sudo()
        self.assertEqual(picking.state, 'done')

    def test_enforcing_needs_a_position_at_all(self):
        self.picking_type.x_location_rule = 'enforce'
        with self.assertRaises(UserError):
            self._issue_note(gps_latitude=0.0, gps_longitude=0.0)

    def test_a_client_location_is_learned_from_its_notes(self):
        for _i in range(3):
            self._issue_note()
        # One note issued from the depot by mistake, far away. The median has
        # to ignore it; a mean would drag the site across town.
        self._issue_note(gps_latitude=FAR[0], gps_longitude=FAR[1])

        self.customer.action_set_location_from_deliveries()
        self.assertAlmostEqual(self.customer.partner_latitude, HERE[0], places=3)

    def test_locating_a_client_with_no_notes_says_so(self):
        with self.assertRaises(UserError):
            self.unrated_customer.action_set_location_from_deliveries()


@tagged('post_install', '-at_install')
class TestWaterDeliveryGuards(WaterDeliveryCommon):

    def test_a_signature_is_not_optional(self):
        with self.assertRaises(UserError):
            self._issue_note(signature=False)

    def test_zero_quantity_is_refused(self):
        with self.assertRaises(UserError):
            self._issue_note(entered_qty=0.0)

    def test_a_double_tap_cannot_issue_two_notes(self):
        wizard = self._issue_note()
        with self.assertRaises(UserError):
            wizard.action_confirm()

    def test_a_driver_cannot_record_another_drivers_delivery(self):
        with self.assertRaises(UserError):
            self._issue_note(driver_id=self.other_driver.id)

    def test_a_user_outside_the_groups_cannot_issue_a_note(self):
        outsider = self.env['res.users'].create({
            'name': 'Outsider', 'login': 'outsider@ksw.test',
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.env['hr.employee'].create({'name': 'Outsider', 'user_id': outsider.id})
        with self.assertRaises(UserError):
            self._issue_note(user=outsider)

    def test_a_dispatcher_may_issue_on_a_drivers_behalf(self):
        dispatcher = self.env['res.users'].create({
            'name': 'Dispatcher', 'login': 'dispatcher@ksw.test',
            'group_ids': [(6, 0, [
                self.env.ref('KSW_water_delivery.group_water_dispatcher').id,
            ])],
        })
        self.env['hr.employee'].create({'name': 'Dispatcher', 'user_id': dispatcher.id})
        wizard = self._issue_note(user=dispatcher, driver_id=self.driver.id)
        self.assertEqual(wizard.picking_id.sudo().x_driver_id, self.driver)

    def test_a_driver_cannot_change_a_rate(self):
        with self.assertRaises(Exception):
            self.env['ksw.water.rate'].with_user(self.driver_user).browse(
                self.rate.id).write({'price': 1.0})


@tagged('post_install', '-at_install')
class TestWaterDeliveryScope(WaterDeliveryCommon):

    def test_a_driver_sees_only_his_own_notes(self):
        mine = self._issue_note(user=self.driver_user).picking_id
        theirs = self._issue_note(
            user=self.other_driver_user, partner_id=self.multi_customer.id,
        ).picking_id

        visible = self.env['stock.picking'].with_user(self.driver_user).search(
            [('x_is_water_delivery', '=', True)])
        self.assertNotIn(theirs.id, visible.ids)
        self.assertIn(mine.id, visible.ids)

    def test_a_driver_can_open_his_own_note(self):
        picking = self._issue_note().picking_id
        data = picking.with_user(self.driver_user).web_read({
            'name': {}, 'partner_id': {'fields': {'display_name': {}}},
            'x_load_qty': {}, 'x_amount_total': {}, 'x_signed_by': {},
            'x_entered_qty': {}, 'x_entered_uom': {}, 'x_gps_distance_m': {},
            'x_currency_id': {'fields': {'display_name': {}}},
        })
        self.assertEqual(data[0]['x_amount_total'], 230.0)


@tagged('post_install', '-at_install')
class TestWaterDeliveryMonthEnd(WaterDeliveryCommon):

    def test_a_month_of_notes_becomes_one_invoice(self):
        for _i in range(3):
            self._issue_note(entered_qty=2.0)

        orders = self.env['sale.order'].sudo().search([
            ('partner_id', '=', self.customer.id), ('state', '=', 'sale')])
        self.assertEqual(len(orders), 3)

        wizard = self.env['sale.advance.payment.inv'].sudo().with_context(
            active_model='sale.order', active_ids=orders.ids,
        ).create({'advance_payment_method': 'delivered', 'consolidated_billing': True})
        wizard.create_invoices()

        invoices = orders.invoice_ids
        self.assertEqual(len(invoices), 1)
        self.assertEqual(len(invoices.invoice_line_ids), 3)
        self.assertEqual(invoices.amount_total, 3 * 2 * 200.0 * 1.15)

        invoice_date = fields.Date.today()
        invoices.write({'invoice_date': invoice_date})
        invoices.action_post()
        self.assertEqual(invoices.state, 'posted')
        self.assertEqual(invoices.invoice_date_due,
                         fields.Date.add(invoice_date, days=60))

    def test_an_unsigned_note_is_visible_as_a_gap(self):
        picking = self._issue_note().picking_id.sudo()
        picking.write({'signature': False})

        self.assertFalse(picking.x_signed_on)
        unsigned = self.env['stock.picking'].sudo().search([
            ('x_is_water_delivery', '=', True), ('x_signed_on', '=', False),
            ('x_signature_required', '=', True),
        ])
        self.assertIn(picking.id, unsigned.ids)

    def test_the_native_sign_button_stamps_the_marker_too(self):
        picking = self._issue_note().picking_id.sudo()
        picking.write({'signature': False, 'x_signed_by': False})

        picking.write({'signature': STUB_SIGNATURE})
        self.assertTrue(picking.x_signed_on)
        self.assertEqual(picking.x_signed_by, self.customer.name)


@tagged('post_install', '-at_install')
class TestProofOfConcept(WaterDeliveryCommon):
    """The first rollout runs without signatures: drivers issue the note at the
    client to prove they can, and the paper note still collects the wet
    signature. Signing is switched off per branch, not removed."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.picking_type.x_signature_required = False

    def test_a_driver_can_issue_a_note_without_a_signature(self):
        picking = self._issue_note(
            signature=False, signed_by=False).picking_id.sudo()

        self.assertEqual(picking.state, 'done')
        self.assertFalse(picking.is_signed)
        self.assertEqual(picking.x_amount_total, 230.0,
                         'the note still has to carry its figures')

    def test_the_driver_is_not_asked_for_a_signature(self):
        Wizard = self.env['ksw.water.delivery.wizard'].with_user(self.driver_user)
        form = Wizard.new({'vehicle_id': self.vehicle.id})
        self.assertFalse(form.signature_required)

        self.picking_type.x_signature_required = True
        form = Wizard.new({'vehicle_id': self.vehicle.id})
        self.assertTrue(form.signature_required)

    def test_turning_signing_on_does_not_reclassify_the_pilot_notes(self):
        poc = self._issue_note(signature=False, signed_by=False).picking_id.sudo()
        self.assertFalse(poc.x_signature_required)

        self.picking_type.x_signature_required = True
        poc.invalidate_recordset()
        self.assertFalse(poc.x_signature_required)

        with self.assertRaises(UserError):
            self._issue_note(signature=False, signed_by=False)

    def test_a_poc_note_can_still_be_signed_afterwards(self):
        poc = self._issue_note(signature=False, signed_by=False).picking_id.sudo()
        poc.write({'signature': STUB_SIGNATURE})

        self.assertTrue(poc.is_signed)
        self.assertEqual(poc.x_signed_by, self.customer.name)

    def test_the_note_still_renders_without_a_signature(self):
        poc = self._issue_note(signature=False, signed_by=False).picking_id.sudo()
        html = self.env.ref('stock.action_report_delivery').with_user(
            self.driver_user,
        )._render_qweb_html('stock.report_deliveryslip', poc.ids)[0].decode()

        self.assertIn('water_note_table', html)
        self.assertIn('الإجمالي بعد الضريبة', html)
        # Not `data:image/png;base64` -- the company logo in the header is one
        # of those too. Core's signature block is what has to be absent.
        self.assertNotIn('name="signature"', html)


@tagged('post_install', '-at_install')
class TestWaterDeliveryClientScope(WaterDeliveryCommon):
    """Showing every client to every driver was the complaint. The list is
    narrowed by branch, then optionally by the driver's own client list — and
    a missing list never narrows, or a branch nobody has listed yet would lock
    its drivers out."""

    def _allowed(self, user=None, **vals):
        user = user or self.driver_user
        Wizard = self.env['ksw.water.delivery.wizard'].with_user(user)
        defaults = Wizard.default_get(list(Wizard._fields))
        defaults.update(dict(vehicle_id=self.vehicle.id, **vals))
        return Wizard.new(defaults).allowed_partner_ids._origin

    def test_the_branch_list_is_the_default(self):
        allowed = self._allowed()

        self.assertIn(self.customer.id, allowed.ids)
        self.assertIn(self.multi_customer.id, allowed.ids)
        # Priced, but another branch's client: the whole point of the change.
        self.assertNotIn(self.other_branch_customer.id, allowed.ids)
        self.assertNotIn(self.unrated_customer.id, allowed.ids)

    def test_a_drivers_own_list_narrows_it_further(self):
        self.driver.sudo().x_water_client_ids = [(6, 0, self.customer.ids)]
        allowed = self._allowed()

        self.assertEqual(allowed, self.customer)
        self.assertNotIn(self.multi_customer.id, allowed.ids,
                         'his own list has to beat the branch list')

    def test_a_drivers_list_cannot_widen_past_what_is_priced(self):
        """It narrows; it is not an override. A client with no rate still
        cannot be delivered to, because nothing could price the note."""
        self.driver.sudo().x_water_client_ids = [
            (6, 0, (self.customer | self.unrated_customer).ids)]
        allowed = self._allowed()

        self.assertIn(self.customer.id, allowed.ids)
        self.assertNotIn(self.unrated_customer.id, allowed.ids)

    def test_a_branch_nobody_has_listed_shows_every_priced_client(self):
        """An empty list means "nobody has said yet", not "this branch serves
        nobody" — otherwise switching a new branch on locks it out."""
        self.picking_type.x_branch_code = '404'
        allowed = self._allowed()

        self.assertIn(self.customer.id, allowed.ids)
        self.assertIn(self.other_branch_customer.id, allowed.ids)
        self.assertNotIn(self.unrated_customer.id, allowed.ids)

    def test_the_driver_can_still_reach_an_unlisted_client(self):
        """A narrowed picker that cannot be escaped is a driver stuck at a
        gate."""
        allowed = self._allowed(show_all_clients=True)
        self.assertIn(self.other_branch_customer.id, allowed.ids)

    def test_going_off_the_usual_route_is_recorded_on_the_note(self):
        picking = self._issue_note(
            partner_id=self.other_branch_customer.id, show_all_clients=True,
        ).picking_id.sudo()

        self.assertTrue(picking.x_off_route)
        off_route = self.env['stock.picking'].sudo().search([
            ('x_is_water_delivery', '=', True), ('x_off_route', '=', True)])
        self.assertIn(picking.id, off_route.ids)

    def test_a_normal_client_is_not_flagged_even_with_the_tick_on(self):
        """The flag is worked out from the lists, not from the tick box —
        ticking it and then picking a normal client is not an exception."""
        picking = self._issue_note(show_all_clients=True).picking_id.sudo()
        self.assertFalse(picking.x_off_route)

    def test_a_driver_list_makes_an_in_branch_client_off_route(self):
        self.driver.sudo().x_water_client_ids = [(6, 0, self.customer.ids)]
        picking = self._issue_note(
            partner_id=self.multi_customer.id, show_all_clients=True,
        ).picking_id.sudo()
        self.assertTrue(picking.x_off_route)

    def test_an_ordinary_employee_can_still_read_their_own_record(self):
        """The m2m lives on hr.employee with no model-level groups=. That is
        safe only because it is not a stored column — KSW gotcha #32 is about
        stored fields breaking the hr.employee.public fetch path. Assert it,
        rather than reasoning about it."""
        data = self.driver.with_user(self.driver_user).read(['name'])
        self.assertTrue(data)


@tagged('post_install', '-at_install')
class TestWaterDeliveryProductFallback(WaterDeliveryCommon):
    """The web client omits read-only fields from its payload, so the one-product
    case arrived with no `product_id` and died on a NOT NULL constraint — the
    driver's phone showed a raw "mandatory field is not set". The field must
    therefore be fillable server-side, not merely computed for the screen."""

    def test_creating_without_a_product_fills_the_only_one(self):
        wizard = self.env['ksw.water.delivery.wizard'].with_user(
            self.driver_user,
        ).create({
            'partner_id': self.customer.id,
            'entered_qty': 1.0,
            'driver_id': self.driver.id,
            'vehicle_id': self.vehicle.id,
            # product_id deliberately absent, exactly as the browser sends it
        })
        self.assertEqual(wizard.product_id, self.water)

    def test_the_whole_flow_survives_the_missing_product(self):
        wizard = self.env['ksw.water.delivery.wizard'].with_user(
            self.driver_user,
        ).create({
            'partner_id': self.customer.id,
            'entered_qty': 1.0,
            'signature': STUB_SIGNATURE,
            'signed_by': 'Site Foreman',
            'driver_id': self.driver.id,
            'vehicle_id': self.vehicle.id,
        })
        wizard.action_confirm()
        self.assertEqual(wizard.picking_id.sudo().state, 'done')

    def test_a_client_with_a_choice_is_not_guessed_for(self):
        """Auto-filling only applies where there is one answer. With a real
        choice, an empty value is a genuine omission and must not be papered
        over by picking whichever product sorts first."""
        with self.assertRaises(Exception):
            self.env['ksw.water.delivery.wizard'].with_user(
                self.driver_user,
            ).create({
                'partner_id': self.multi_customer.id,
                'entered_qty': 1.0,
                'driver_id': self.driver.id,
                'vehicle_id': self.vehicle.id,
            })

    def test_the_form_round_trip_a_driver_actually_does(self):
        """End to end through `Form`, which respects the same read-only rules
        the browser does."""
        form = Form(self.env['ksw.water.delivery.wizard'].with_user(self.driver_user))
        form.vehicle_id = self.vehicle
        form.partner_id = self.customer
        form.entered_qty = 2.0
        form.signed_by = 'Site Foreman'
        wizard = form.save()
        wizard.signature = STUB_SIGNATURE
        wizard.action_confirm()

        picking = wizard.picking_id.sudo()
        self.assertEqual(picking.state, 'done')
        self.assertEqual(picking.sale_id.order_line.product_id, self.water)
        self.assertEqual(picking.x_amount_untaxed, 400.0)
