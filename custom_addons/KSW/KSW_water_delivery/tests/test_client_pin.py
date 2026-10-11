"""A client's location: sent by a driver from the site, approved by the office.

Under the enforce rule the approved location is the gate -- no note at all for
a client without one -- so the guarded properties are: a driver's pin never
locates a client by itself, only a dispatcher's approval does, a dispatcher
cannot approve his own pin, and once approved the client is reachable only
within the radius.
"""
from odoo.exceptions import UserError
from odoo.tests import tagged

from .test_water_delivery import FAR, HERE, WaterDeliveryCommon


@tagged('post_install', '-at_install')
class TestClientLocationApproval(WaterDeliveryCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.picking_type.x_location_rule = 'enforce'
        dispatcher = cls.env.ref('KSW_water_delivery.group_water_dispatcher')
        cls.dispatcher_user = cls.env['res.users'].create({
            'name': 'Dispatch Desk', 'login': 'pin-dispatch@ksw.test',
            'group_ids': [(6, 0, [dispatcher.id])],
        })
        supervisor = cls.env['hr.employee'].create({
            'name': 'Dispatch Desk', 'user_id': cls.dispatcher_user.id})
        # A supervisor sees his team only (hr.employee.parent_id).
        (cls.driver | cls.other_driver).write({'parent_id': supervisor.id})
        cls.Request = cls.env['ksw.water.location.request']

    def _pin(self, partner=None, lat=HERE[0], lon=HERE[1], accuracy=12.0, ref='p1'):
        return {
            'uuid': 'pin-%s' % ref,
            'partner_id': (partner or self.customer).id,
            'latitude': lat, 'longitude': lon, 'accuracy': accuracy,
            'captured_at': '2026-10-11 08:00:00', 'app_version': '1.0.4',
        }

    def _send(self, *pins, driver=None):
        return self.env['res.partner'].sudo()._water_receive_pins(
            list(pins), driver or self.driver)

    def _request(self, ref='p1'):
        return self.Request.sudo().search([('uuid', '=', 'pin-%s' % ref)])

    # -- the pin is a request, not a location ---------------------------

    def test_a_pin_waits_for_approval(self):
        [result] = self._send(self._pin())

        self.assertEqual(result['status'], 'pending')
        self.assertTrue(result['settled'])
        self.assertFalse(self.customer.partner_latitude,
                         "a driver's pin must not locate the client by itself")
        request = self._request()
        self.assertEqual(request.state, 'pending')
        self.assertEqual(request.employee_id, self.driver)
        self.assertAlmostEqual(request.phone_latitude, HERE[0], places=5)

    def test_a_resent_pin_is_not_queued_twice(self):
        self._send(self._pin())
        [again] = self._send(self._pin())
        self.assertEqual(again['status'], 'pending')
        self.assertEqual(self.Request.sudo().search_count(
            [('partner_id', '=', self.customer.id)]), 1)

    def test_no_note_while_the_location_waits(self):
        self._send(self._pin())
        with self.assertRaisesRegex(UserError, 'no approved location'):
            self._issue_note()

    # -- approval -------------------------------------------------------

    def test_approval_locates_the_client_and_opens_delivery(self):
        self._send(self._pin())
        self._request().with_user(self.dispatcher_user).action_approve()

        self.assertAlmostEqual(self.customer.partner_latitude, HERE[0], places=5)
        self.assertEqual(self._request().state, 'approved')
        self.assertEqual(self._request().reviewed_by, self.dispatcher_user)
        picking = self._issue_note().picking_id.sudo()
        self.assertEqual(picking.state, 'done')

    def test_after_approval_a_note_far_away_is_refused(self):
        self._send(self._pin())
        self._request().with_user(self.dispatcher_user).action_approve()
        with self.assertRaises(UserError):
            self._issue_note(gps_latitude=FAR[0], gps_longitude=FAR[1])

    def test_the_dispatcher_may_correct_the_pin_before_approving(self):
        """The approved figure is what reaches the client; the phone's reading
        is kept beside it."""
        self._send(self._pin())
        request = self._request().with_user(self.dispatcher_user)
        request.write({'latitude': HERE[0] + 0.0005})
        request.action_approve()

        self.assertAlmostEqual(self.customer.partner_latitude, HERE[0] + 0.0005, places=6)
        self.assertAlmostEqual(request.phone_latitude, HERE[0], places=6)

    def test_approving_one_answers_the_others(self):
        self._send(self._pin(ref='a'))
        self._send(self._pin(lat=FAR[0], ref='b'), driver=self.other_driver)
        self._request('a').with_user(self.dispatcher_user).action_approve()

        self.assertEqual(self._request('b').state, 'rejected')
        self.assertTrue(self._request('b').reject_reason)

    def test_a_dispatcher_cannot_approve_his_own_pin(self):
        """Dispatchers are drivers too; the approval is only worth something
        if it is somebody else's."""
        dispatcher_employee = self.dispatcher_user.employee_id
        self.vehicle.driver_id = dispatcher_employee
        self._send(self._pin(), driver=dispatcher_employee)
        with self.assertRaisesRegex(UserError, 'another supervisor'):
            self._request().with_user(self.dispatcher_user).action_approve()
        self.assertFalse(self.customer.partner_latitude)

    def test_a_driver_cannot_approve(self):
        self._send(self._pin(), driver=self.other_driver)
        with self.assertRaises(UserError):
            self._request().sudo(False).with_user(self.driver_user)._check_reviewer()

    def test_rejecting_needs_a_reason(self):
        self._send(self._pin())
        request = self._request().with_user(self.dispatcher_user)
        with self.assertRaisesRegex(UserError, 'reason'):
            request.action_reject()
        request.write({'reject_reason': 'That is the depot.'})
        request.action_reject()
        self.assertEqual(request.state, 'rejected')
        self.assertFalse(self.customer.partner_latitude)

    # -- what the phone may send at all ---------------------------------

    def test_a_located_client_takes_no_more_pins(self):
        self.customer.write({'partner_latitude': HERE[0], 'partner_longitude': HERE[1]})
        [result] = self._send(self._pin(lat=FAR[0]))
        self.assertEqual(result['status'], 'already')
        self.assertFalse(self._request())

    def test_a_rough_fix_is_refused(self):
        """The approved pin is the centre of the radius: a cell-tower fix
        should not even reach the office."""
        [result] = self._send(self._pin(accuracy=400.0))
        self.assertEqual(result['status'], 'refused')
        self.assertTrue(result['settled'])
        self.assertFalse(self._request())

    def test_nonsense_coordinates_are_refused(self):
        [zero] = self._send(self._pin(lat=0.0, lon=0.0, ref='z'))
        [bad] = self._send(self._pin(lat=123.0, ref='b'))
        self.assertEqual(zero['status'], 'refused')
        self.assertEqual(bad['status'], 'refused')

    def test_a_client_outside_his_scope_cannot_be_pinned(self):
        [result] = self._send(self._pin(partner=self.unrated_customer))
        self.assertEqual(result['status'], 'refused')

    def test_one_bad_pin_does_not_hold_back_the_rest(self):
        results = self._send(
            self._pin(partner=self.unrated_customer, ref='a'),
            self._pin(partner=self.multi_customer, ref='b'),
        )
        self.assertEqual([r['status'] for r in results], ['refused', 'pending'])

    def test_the_phone_is_told_which_clients_are_waiting(self):
        self._send(self._pin())
        pending = self.Request._pending_partner_ids(
            [self.customer.id, self.multi_customer.id])
        self.assertEqual(pending, {self.customer.id})


@tagged('post_install', '-at_install')
class TestClientLocationOnTheForm(TestClientLocationApproval):
    """The same request, from the Odoo New Delivery form's 📍 button.

    Added after a driver on that form was refused ("no approved location")
    with no way to send one -- the button existed only in the offline app."""

    def _form_send(self, user=None, partner=None, accuracy=12.0):
        Wizard = self.env['ksw.water.delivery.wizard'].with_user(user or self.driver_user)
        return Wizard.action_send_client_location(
            (partner or self.customer).id, HERE[0], HERE[1], accuracy)

    def test_the_form_button_sends_a_request(self):
        result = self._form_send()
        self.assertEqual(result['status'], 'pending')
        self.assertFalse(self.customer.partner_latitude)
        self.assertEqual(self.Request.sudo().search_count(
            [('partner_id', '=', self.customer.id), ('state', '=', 'pending')]), 1)

    def test_the_form_knows_where_the_client_stands(self):
        Wizard = self.env['ksw.water.delivery.wizard'].with_user(self.driver_user)
        wizard = Wizard.new({'partner_id': self.customer.id})
        self.assertEqual(wizard.partner_location_state, 'missing')
        self._form_send()
        wizard = Wizard.new({'partner_id': self.customer.id})
        self.assertEqual(wizard.partner_location_state, 'pending')
        self.Request.sudo().search([]).with_user(self.dispatcher_user).action_approve()
        wizard = Wizard.new({'partner_id': self.customer.id})
        self.assertEqual(wizard.partner_location_state, 'located')

    def test_a_rough_fix_from_the_form_is_refused(self):
        self.assertEqual(self._form_send(accuracy=300.0)['status'], 'refused')

    def test_only_drivers_send_locations(self):
        billing = self.env['res.users'].create({
            'name': 'Billing Only', 'login': 'pin-billing@ksw.test',
            'group_ids': [(6, 0, [
                self.env.ref('KSW_water_delivery.group_water_billing').id])],
        })
        with self.assertRaises(UserError):
            self._form_send(user=billing)
