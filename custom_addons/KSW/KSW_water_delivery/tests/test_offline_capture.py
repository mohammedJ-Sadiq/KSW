"""The offline queue: capture at the client, issue when the truck is back.

What these are actually guarding, in the order it matters:

1. One load of water makes one delivery note, however many times the phone
   sends it. A retry on the edge of coverage is normal, not exceptional.
2. A delivery that has already happened is never lost. Not when the location
   cannot be confirmed, not when nobody put a rate in -- the record exists and
   somebody can see it.
3. The note is priced by the server, from the live register, at the moment it
   is issued. Never from what the phone was holding.
4. A held note cannot be invoiced until a human says so -- and that is enforced
   by it not being validated, not by a flag somebody has to notice.
"""
import base64
import json

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import HttpCase, tagged

from .test_water_delivery import FAR, HERE, STUB_SIGNATURE, WaterDeliveryCommon


@tagged('post_install', '-at_install')
class OfflineCaptureCommon(WaterDeliveryCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.dispatcher_user = cls.env['res.users'].create({
            'name': 'Dispatch Desk',
            'login': 'dispatch@ksw.test',
            'group_ids': [(6, 0, [
                cls.env.ref('KSW_water_delivery.group_water_dispatcher').id,
            ])],
        })
        cls.env['hr.employee'].create({
            'name': 'Dispatch Desk', 'user_id': cls.dispatcher_user.id,
        })
        cls.Capture = cls.env['ksw.water.capture'].sudo()

    def _payload(self, **extra):
        """What the phone puts in IndexedDB and posts back later."""
        values = {
            'uuid': 'cap-%s' % (extra.pop('ref', '0001')),
            'partner_id': self.customer.id,
            'product_id': self.water.id,
            'entered_qty': 2.0,
            'entered_uom': 'product',
            'vehicle_id': self.vehicle.id,
            'signature': STUB_SIGNATURE,
            'signed_by': 'Site Foreman',
            'signature_origin': 'on_site',
            'note': False,
            'off_route': False,
            'gps_latitude': HERE[0],
            'gps_longitude': HERE[1],
            'gps_accuracy': 12.0,
            'captured_at': '2026-09-20 06:30:00',
            'sent_at': fields.Datetime.to_string(fields.Datetime.now()),
            'cached_price': 200.0,
            'app_version': '1.0.0',
        }
        values.update(extra)
        return values

    def _receive(self, **extra):
        return self.Capture._receive(self._payload(**extra), self.driver)

    def _enforce_here(self):
        """Turn on the rule that only bites once clients have coordinates."""
        self.picking_type.x_location_rule = 'enforce'
        self.customer.write({
            'partner_latitude': HERE[0], 'partner_longitude': HERE[1],
        })


@tagged('post_install', '-at_install')
class TestOfflineIssue(OfflineCaptureCommon):

    def test_a_capture_becomes_a_real_note(self):
        """The offline path produces the same native chain as the live one --
        that is the whole reason it calls the same method."""
        capture = self._receive()

        self.assertEqual(capture.state, 'issued')
        picking = capture.picking_id.sudo()
        self.assertEqual(picking.state, 'done')
        self.assertTrue(picking.is_signed)
        self.assertTrue(picking.name.startswith('WH/WTR/'))
        self.assertEqual(picking.sale_id.order_line.qty_delivered, 2.0)
        self.assertEqual(picking.x_driver_id, self.driver)

    def test_the_note_records_that_it_came_from_the_queue(self):
        """`x_issued_offline` and `x_captured_at` are history: they say how the
        note came to exist, which stays true forever."""
        picking = self._receive().picking_id.sudo()

        self.assertTrue(picking.x_issued_offline)
        self.assertEqual(
            fields.Datetime.to_string(picking.x_captured_at), '2026-09-20 06:30:00')
        self.assertEqual(picking.x_capture_id.uuid, 'cap-0001')

    def test_a_live_note_is_not_marked_offline(self):
        """The negative half, asserted first because it is the one that would
        silently rot: the wizard must not start stamping every note offline."""
        picking = self._issue_note().picking_id.sudo()
        self.assertFalse(picking.x_issued_offline)
        self.assertFalse(picking.x_captured_at)
        self.assertFalse(picking.x_location_exception)

    def test_the_three_timestamps_are_kept_apart(self):
        """Offline capture moves the clock under the driver's control, so the
        phone's time and the server's arrival time are never merged into one
        'date' that reads as authoritative."""
        capture = self._receive()

        self.assertNotEqual(capture.captured_at, capture.received_at)
        self.assertGreater(capture.queued_seconds, 0)
        # Sent with an honest clock, so no skew worth reporting.
        self.assertLess(abs(capture.clock_skew_s), 60)

    def test_a_long_queue_is_not_mistaken_for_a_wrong_clock(self):
        """The trap this separation exists for: a capture that sat in a pocket
        for six hours is offline capture WORKING. Measuring skew against
        `captured_at` would flag every single one."""
        capture = self._receive(captured_at='2026-09-20 01:00:00')

        self.assertGreater(capture.queued_seconds, 3600)
        self.assertLess(abs(capture.clock_skew_s), 60)

    def test_a_wrong_clock_is_reported(self):
        """A device clock is settable, so `captured_at` is evidence only as
        far as the clock behind it can be trusted. Say so on the record rather
        than quietly believing it."""
        capture = self._receive(sent_at='2026-09-21 06:30:00')

        self.assertGreater(abs(capture.clock_skew_s), 3600)
        self.assertTrue(capture.message_ids.filtered(
            lambda m: 'clock' in (m.body or '')))


@tagged('post_install', '-at_install')
class TestIdempotency(OfflineCaptureCommon):

    def test_the_same_capture_twice_makes_one_note(self):
        """The one that protects the money. A response lost on a flaky edge of
        coverage is indistinguishable from a send that never arrived, so the
        phone WILL resend -- and one load of water must not become two notes
        and two invoice lines."""
        first = self._receive(ref='dup')
        second = self._receive(ref='dup')

        self.assertEqual(first, second)
        self.assertEqual(self.Capture.search_count([('uuid', '=', 'cap-dup')]), 1)
        notes = self.env['stock.picking'].sudo().search([
            ('x_capture_id', '=', first.id)])
        self.assertEqual(len(notes), 1)

    def test_a_resend_does_not_reopen_a_reviewed_capture(self):
        self._enforce_here()
        capture = self._receive(ref='held', gps_latitude=FAR[0], gps_longitude=FAR[1])
        capture.with_user(self.dispatcher_user).action_accept()
        self.assertEqual(capture.state, 'issued')

        again = self._receive(ref='held')
        self.assertEqual(again, capture)
        self.assertEqual(again.state, 'issued')


@tagged('post_install', '-at_install')
class TestLocationHold(OfflineCaptureCommon):

    def test_out_of_radius_is_held_not_refused(self):
        """The delivery has already happened. Refusing would produce water in a
        customer's tank with no document against it, which is the exact failure
        the module was built to remove."""
        self._enforce_here()
        capture = self._receive(gps_latitude=FAR[0], gps_longitude=FAR[1])

        self.assertEqual(capture.state, 'held')
        self.assertTrue(capture.hold_reason)
        picking = capture.picking_id.sudo()
        self.assertTrue(picking, 'the note must exist -- the water was delivered')
        self.assertTrue(picking.x_location_exception)
        self.assertGreater(picking.x_gps_distance_m, 300)

    def test_a_held_note_cannot_be_invoiced(self):
        """And this is what gives the rule teeth without inventing a reversal:
        `invoice_policy='delivery'` bills delivered quantity, and a note nobody
        has validated has delivered nothing."""
        self._enforce_here()
        capture = self._receive(gps_latitude=FAR[0], gps_longitude=FAR[1])

        picking = capture.picking_id.sudo()
        self.assertNotEqual(picking.state, 'done')
        self.assertEqual(picking.sale_id.order_line.qty_delivered, 0.0)
        self.assertEqual(picking.sale_id.invoice_status, 'no')
        # Not signed either: core renders the PDF the moment the signature is
        # written, so signing before validation would attach a document showing
        # quantities nobody had confirmed.
        self.assertFalse(picking.x_signed_on)

    def test_no_position_at_all_is_held(self):
        """Weaker evidence than a wrong position, not stronger: there is
        nothing at all to say the driver was anywhere."""
        self._enforce_here()
        capture = self._receive(gps_latitude=0.0, gps_longitude=0.0)

        self.assertEqual(capture.state, 'held')
        self.assertIn('No position', capture.hold_reason)

    def test_an_unlocated_client_stays_reachable(self):
        """Enforcement must not lock out every site until somebody surveys it
        -- the surveying is done BY issuing notes there."""
        self.picking_type.x_location_rule = 'enforce'
        self.assertFalse(self.customer.partner_latitude)
        capture = self._receive(gps_latitude=FAR[0], gps_longitude=FAR[1])
        self.assertEqual(capture.state, 'issued')

    def test_the_pilot_branch_issues_everything(self):
        """While the branch is only recording positions rather than enforcing
        them, nothing is held. This is the state production is actually in."""
        self.picking_type.x_location_rule = 'capture'
        self.customer.write({
            'partner_latitude': HERE[0], 'partner_longitude': HERE[1]})
        capture = self._receive(gps_latitude=FAR[0], gps_longitude=FAR[1])

        self.assertEqual(capture.state, 'issued')
        self.assertFalse(capture.picking_id.sudo().x_location_exception)
        # Still measured, because measuring is what makes enforcing possible.
        self.assertGreater(capture.picking_id.sudo().x_gps_distance_m, 300)


@tagged('post_install', '-at_install')
class TestDispatcherReview(OfflineCaptureCommon):

    def setUp(self):
        super().setUp()
        self._enforce_here()
        self.capture = self._receive(gps_latitude=FAR[0], gps_longitude=FAR[1])

    def test_accepting_validates_and_signs(self):
        self.capture.with_user(self.dispatcher_user).action_accept()

        self.assertEqual(self.capture.state, 'issued')
        picking = self.capture.picking_id.sudo()
        self.assertEqual(picking.state, 'done')
        self.assertTrue(picking.is_signed)
        self.assertEqual(picking.x_signed_by, 'Site Foreman')
        self.assertEqual(picking.sale_id.order_line.qty_delivered, 2.0)
        # The question has been answered, so the flag goes; how the note came
        # to exist does not change.
        self.assertFalse(picking.x_location_exception)
        self.assertTrue(picking.x_issued_offline)

    def test_rejecting_cancels_the_note(self):
        self.capture.review_note = 'Driver confirmed he picked the wrong client.'
        self.capture.with_user(self.dispatcher_user).action_reject()

        self.assertEqual(self.capture.state, 'rejected')
        self.assertEqual(self.capture.picking_id.sudo().state, 'cancel')

    def test_rejecting_needs_a_reason(self):
        """The driver recorded a delivery. Cancelling it without saying why
        turns a record into a disagreement nobody can settle later."""
        with self.assertRaises(UserError):
            self.capture.with_user(self.dispatcher_user).action_reject()

    def test_a_driver_cannot_review_his_own_exception(self):
        """The guard is server-side, not the button's `groups=`: a view-level
        restriction is cosmetic against a direct RPC (KSW gotcha #15)."""
        with self.assertRaises(UserError):
            self.capture.with_user(self.driver_user).action_accept()

    def test_a_driver_sees_only_his_own_captures(self):
        other = self.Capture._receive(
            self._payload(ref='other'), self.other_driver)

        visible = self.env['ksw.water.capture'].with_user(self.driver_user).search([])
        self.assertIn(self.capture, visible)
        self.assertNotIn(other, visible)


@tagged('post_install', '-at_install')
class TestNothingIsLost(OfflineCaptureCommon):

    def test_a_missing_rate_fails_without_losing_the_delivery(self):
        """Losing a delivery because accounting had not entered a price is
        exactly the failure the paper process already has."""
        capture = self._receive(partner_id=self.unrated_customer.id)

        self.assertEqual(capture.state, 'failed')
        self.assertIn('rate', capture.failure_reason)
        self.assertFalse(capture.picking_id)
        # It is HERE, where a dispatcher can find it, not on a phone.
        self.assertTrue(capture.exists())

    def test_a_failed_capture_is_retried_once_the_data_is_fixed(self):
        capture = self._receive(partner_id=self.unrated_customer.id)
        self.env['ksw.water.rate'].create({
            'partner_id': self.unrated_customer.id,
            'product_id': self.water.id, 'price': 175.0,
        })

        capture.with_user(self.dispatcher_user).action_retry()

        self.assertEqual(capture.state, 'issued')
        self.assertEqual(capture.picking_id.sudo().sale_id.order_line.price_unit, 175.0)


@tagged('post_install', '-at_install')
class TestPricing(OfflineCaptureCommon):

    def test_the_server_prices_the_note_not_the_phone(self):
        """A phone carrying a three-day-old snapshot must never be able to
        invoice a stale rate."""
        capture = self._receive(cached_price=1.0)

        self.assertEqual(capture.picking_id.sudo().sale_id.order_line.price_unit, 200.0)
        self.assertEqual(capture.issued_price, 200.0)
        self.assertEqual(capture.cached_price, 1.0)

    def test_a_rate_that_moved_is_flagged_rather_than_silently_taken(self):
        """The customer signed a screen showing one figure and the note says
        another. The note is right; somebody still has to be told."""
        capture = self._receive(cached_price=1.0)

        self.assertTrue(capture.price_changed)
        self.assertTrue(capture.message_ids.filtered(
            lambda m: 'rate changed' in (m.body or '').lower()))

    def test_an_unchanged_rate_is_not_flagged(self):
        capture = self._receive(cached_price=200.0)
        self.assertFalse(capture.price_changed)


@tagged('post_install', '-at_install')
class TestDriverApp(HttpCase):
    """The routes the phone actually calls.

    Driven over HTTP rather than by calling the controller, because the
    interesting failures here are auth and routing -- the two things a direct
    call would supply for itself.
    """

    def setUp(self):
        super().setUp()
        self.driver_user = self.env['res.users'].create({
            'name': 'App Driver',
            'login': 'app.driver@ksw.test',
            'password': 'app.driver@ksw.test',
            'group_ids': [(6, 0, [
                self.env.ref('KSW_water_delivery.group_water_driver').id,
            ])],
        })
        self.env['hr.employee'].create({
            'name': 'App Driver', 'user_id': self.driver_user.id})

    def _rpc(self, route, params=None):
        response = self.url_open(
            route, data=json.dumps({
                'jsonrpc': '2.0', 'method': 'call', 'params': params or {},
            }), headers={'Content-Type': 'application/json'})
        return response.json()

    def test_the_shell_is_served_and_holds_no_user_data(self):
        """It is cached by a service worker and replayed offline, so it has to
        be the same page for every driver."""
        self.authenticate('app.driver@ksw.test', 'app.driver@ksw.test')
        response = self.url_open('/water/')

        self.assertEqual(response.status_code, 200)
        self.assertIn('ksw-app', response.text)
        self.assertNotIn('App Driver', response.text)

    def test_the_service_worker_is_served_with_a_scope_it_can_use(self):
        """A worker at /water/sw.js may only claim /water/ unless the server
        widens it, and without the header the registration is refused."""
        response = self.url_open('/water/sw.js')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get('Service-Worker-Allowed'), '/water/')
        self.assertNotIn('__APP_VERSION__', response.text)

    def test_the_reference_call_needs_the_driver_group(self):
        outsider = self.env['res.users'].create({
            'name': 'Not A Driver', 'login': 'outsider@ksw.test',
            'password': 'outsider@ksw.test',
        })
        self.env['hr.employee'].create({
            'name': 'Not A Driver', 'user_id': outsider.id})
        self.authenticate('outsider@ksw.test', 'outsider@ksw.test')

        payload = self._rpc('/water/reference')
        self.assertIn('error', payload)

    def test_the_reference_carries_what_the_phone_needs_offline(self):
        self.authenticate('app.driver@ksw.test', 'app.driver@ksw.test')
        result = self._rpc('/water/reference').get('result')

        self.assertTrue(result)
        self.assertEqual(result['driver']['name'], 'App Driver')
        for key in ('clients', 'rules', 'app_version', 'server_time'):
            self.assertIn(key, result)
        self.assertIn('location_rule', result['rules'])
        # Prices ride along so the screen can show one with no connection --
        # display only; the note is priced by the server.
        if result['clients']:
            self.assertIn('price', result['clients'][0]['products'][0])

    def test_the_manifest_makes_it_an_installable_app(self):
        response = self.url_open('/water/manifest.webmanifest')
        manifest = response.json()

        self.assertEqual(manifest['start_url'], '/water/')
        self.assertEqual(manifest['scope'], '/water/')
        self.assertEqual(manifest['display'], 'standalone')

    def test_one_bad_capture_does_not_hold_back_the_rest_of_the_batch(self):
        """Answered per uuid on purpose: the phone needs to know which ones it
        may drop, and a rate nobody entered must not strand four good
        deliveries behind it."""
        self.authenticate('app.driver@ksw.test', 'app.driver@ksw.test')
        result = self._rpc('/water/sync', {'captures': [
            {'uuid': 'broken-1'},          # no client, no product
            {'uuid': 'broken-2'},
        ]}).get('result')

        self.assertEqual(len(result['results']), 2)
        self.assertEqual({r['uuid'] for r in result['results']},
                         {'broken-1', 'broken-2'})

    def test_a_capture_that_was_never_recorded_stays_on_the_phone(self):
        """`settled` is the phone's instruction to forget it. Saying yes for
        something the server did not record is how a delivery disappears."""
        self.authenticate('app.driver@ksw.test', 'app.driver@ksw.test')
        result = self._rpc('/water/sync', {'captures': [{'uuid': 'broken-1'}]})

        entry = result['result']['results'][0]
        self.assertFalse(entry['settled'])
        self.assertEqual(entry['status'], 'error')


@tagged('post_install', '-at_install')
class TestOfflineReadiness(HttpCase):
    """Drive the real app in a real browser.

    This class exists because of a live failure that every other kind of test
    passed through: on `http://192.168.1.207:8070` the app looked fine and did
    nothing offline, because `navigator.serviceWorker` does not exist on a
    plain-http origin. No error, no log, no failing assertion — the
    registration call was simply skipped, and the driver would have found out
    at a customer with no signal.

    Only a browser can answer "did the worker actually register and did it
    actually cache the shell". `HttpCase` serves on localhost, which IS a
    secure context, so the mechanism is testable here even though the LAN
    address it was tried on is not (KSW gotcha #151/#157).
    """

    def setUp(self):
        super().setUp()
        self.driver_user = self.env['res.users'].create({
            'name': 'Browser Driver',
            'login': 'browser.driver@ksw.test',
            'password': 'browser.driver@ksw.test',
            'group_ids': [(6, 0, [
                self.env.ref('KSW_water_delivery.group_water_driver').id,
            ])],
        })
        self.env['hr.employee'].create({
            'name': 'Browser Driver', 'user_id': self.driver_user.id})

    def test_the_worker_registers_and_caches_the_shell(self):
        """The two facts that decide whether the app works at a dead site:
        a worker is controlling the page, and `/water/` is really in the cache.

        Asserted separately on purpose. The shell is precached file by file and
        tolerates an individual failure, so "registered" does not imply
        "will start with the radio off" -- which is exactly the distinction the
        readiness panel has to get right.
        """
        self.browser_js(
            '/water/',
            """
            (async () => {
                if (!window.isSecureContext) {
                    console.error("not a secure context: offline can never work here");
                    return;
                }
                const registration = await navigator.serviceWorker.register(
                    "/water/sw.js", { scope: "/water/" });
                await navigator.serviceWorker.ready;
                if (!registration.active && !registration.installing && !registration.waiting) {
                    console.error("no service worker after register()");
                    return;
                }
                // Give the install handler its precache round trip.
                for (let i = 0; i < 40 && !(await shellCached()); i++) {
                    await new Promise((r) => setTimeout(r, 250));
                }
                if (!(await shellCached())) {
                    console.error("the shell was never cached: the app will not start offline");
                    return;
                }
                console.log("test successful");
            })();

            async function shellCached() {
                const names = await caches.keys();
                const shell = names.find((n) => n.startsWith("ksw-water-shell-"));
                if (!shell) { return false; }
                const cache = await caches.open(shell);
                return Boolean(await cache.match("/water/"));
            }
            """,
            login='browser.driver@ksw.test',
            timeout=90,
        )

    def test_the_app_says_out_loud_when_it_is_ready(self):
        """The readiness panel is the whole point of the fix: a driver has to
        be able to see at the depot whether this phone will work at the next
        site, because the depot is the only place he can still do anything
        about it."""
        self.browser_js(
            '/water/',
            """
            (async () => {
                const deadline = Date.now() + 45000;
                while (Date.now() < deadline) {
                    const panel = document.querySelector(".ksw-ready");
                    if (panel && panel.classList.contains("ksw-ready-good")) {
                        console.log("test successful");
                        return;
                    }
                    await new Promise((r) => setTimeout(r, 400));
                }
                const panel = document.querySelector(".ksw-ready");
                console.error(
                    "the app never reported itself ready: " +
                    (panel ? panel.textContent.trim() : "no readiness panel at all")
                );
            })();
            """,
            login='browser.driver@ksw.test',
            timeout=90,
        )
