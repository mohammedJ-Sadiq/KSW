"""Dispatch mode: the dispatcher starts the delivery, the driver confirms it.

What these guard, in order:

1. It is a switch per branch. A branch left on 'driver' behaves exactly as
   before, and nobody can dispatch on it.
2. The note is made of the dispatcher's order, never the phone's copy of it.
3. One dispatch makes one note, and only its own driver can confirm it.
4. A delivery the driver started himself on a dispatch branch is held for
   the supervisor, not refused (the water may already be in the tank).
"""
import json

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import Form, HttpCase, tagged

from .test_offline_capture import OfflineCaptureCommon
from .test_water_delivery import FAR, HERE


@tagged('post_install', '-at_install')
class TestWaterDispatch(OfflineCaptureCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.dispatch_user = cls.env['res.users'].create({
            'name': 'Branch Desk',
            'login': 'desk@ksw.test',
            'group_ids': [(6, 0, [
                cls.env.ref('KSW_water_delivery.group_water_dispatch').id,
            ])],
        })
        cls.outsider_desk = cls.env['res.users'].create({
            'name': 'Other Desk',
            'login': 'otherdesk@ksw.test',
            'group_ids': [(6, 0, [
                cls.env.ref('KSW_water_delivery.group_water_dispatch').id,
            ])],
        })
        cls.picking_type.write({
            'x_issue_mode': 'dispatch',
            'x_dispatcher_user_ids': [(6, 0, cls.dispatch_user.ids)],
        })
        cls.Dispatch = cls.env['ksw.water.dispatch']

    def _dispatch(self, **extra):
        values = {
            'picking_type_id': self.picking_type.id,
            'partner_id': self.customer.id,
            'entered_qty': 3.0,
            'driver_user_id': self.driver_user.id,
        }
        values.update(extra)
        dispatch = self.Dispatch.with_user(self.dispatch_user).create(values)
        dispatch.action_dispatch()
        return dispatch

    def _confirm(self, dispatch, user=None, **extra):
        """What the phone sends when the driver presses Deliver -- with a
        deliberately wrong copy of the order, to prove the server ignores it.
        Sent as the driver, like the controller: the superuser is exempt from
        the holds."""
        user = user or self.driver_user
        payload = self._payload(
            dispatch_id=dispatch.id, partner_id=self.multi_customer.id,
            product_id=self.water_other.id, entered_qty=99.0, **extra)
        return self.Capture.with_user(user)._receive(payload, user.employee_id)

    def _free(self, **extra):
        return self.Capture.with_user(self.driver_user)._receive(
            self._payload(**extra), self.driver)

    # -- the switch ------------------------------------------------------
    def test_a_driver_branch_cannot_be_dispatched(self):
        self.picking_type.x_issue_mode = 'driver'
        dispatch = self.Dispatch.with_user(self.dispatch_user).create({
            'picking_type_id': self.picking_type.id,
            'partner_id': self.customer.id,
            'driver_user_id': self.driver_user.id,
        })
        with self.assertRaises(UserError):
            dispatch.action_dispatch()

    def test_a_driver_branch_still_takes_free_deliveries(self):
        self.picking_type.x_issue_mode = 'driver'
        capture = self._free()
        self.assertEqual(capture.state, 'issued')
        self.assertFalse(capture.picking_id.x_location_exception)

    def test_a_free_delivery_on_a_dispatch_branch_is_held(self):
        capture = self._free()
        self.assertEqual(capture.state, 'held')
        self.assertIn('not dispatched', capture.hold_reason)
        self.assertNotEqual(capture.picking_id.state, 'done')

    # -- the dispatcher's form -------------------------------------------
    def test_the_form_preselects_and_fetches(self):
        """As the dispatcher, through Form (onchanges + read-only like the
        browser): the one product is chosen for him and the tanker is the
        driver's own, never picked."""
        self.env.invalidate_all()
        with Form(self.Dispatch.with_user(self.dispatch_user)) as form:
            self.assertEqual(form.picking_type_id, self.picking_type)
            form.partner_id = self.customer
            self.assertEqual(form.product_id, self.water)
            form.driver_user_id = self.driver_user
            self.assertEqual(form.vehicle_id, self.vehicle)
        dispatch = form.record
        self.assertEqual(dispatch.product_id, self.water)
        self.assertEqual(dispatch.driver_id, self.driver)

    def test_only_drivers_of_the_branch_are_offered(self):
        dispatch = self.Dispatch.with_user(self.dispatch_user).new({
            'picking_type_id': self.picking_type.id})
        offered = dispatch.allowed_driver_user_ids._origin.ids
        self.assertIn(self.driver_user.id, offered)
        # Driver Two has no tanker at all.
        self.assertNotIn(self.other_driver_user.id, offered)

    def test_a_dispatcher_works_only_his_branches(self):
        with self.assertRaises(UserError):
            self.Dispatch.with_user(self.outsider_desk).create({
                'picking_type_id': self.picking_type.id,
                'partner_id': self.customer.id,
                'driver_user_id': self.driver_user.id,
            })

    def test_dispatching_numbers_and_stamps_it(self):
        dispatch = self._dispatch()
        self.assertEqual(dispatch.state, 'dispatched')
        self.assertTrue(dispatch.name.startswith('DSP/'))
        self.assertEqual(dispatch.vehicle_id, self.vehicle)
        self.assertEqual(dispatch.dispatcher_id, self.dispatch_user)
        self.assertIn(dispatch, self.Dispatch._water_open_for_driver(self.driver))
        self.assertNotIn(dispatch, self.Dispatch._water_open_for_driver(self.other_driver))

    def test_a_dispatched_order_is_frozen(self):
        dispatch = self._dispatch()
        with self.assertRaises(UserError):
            dispatch.with_user(self.dispatch_user).write({'entered_qty': 5.0})

    # -- the driver's one button -----------------------------------------
    def test_the_note_is_made_of_the_dispatch(self):
        dispatch = self._dispatch()
        capture = self._confirm(dispatch)

        self.assertEqual(capture.state, 'issued')
        picking = capture.picking_id.sudo()
        self.assertEqual(picking.state, 'done')
        self.assertEqual(picking.partner_id, self.customer)
        self.assertEqual(picking.move_ids.product_id, self.water)
        self.assertEqual(picking.x_load_qty, 3.0)
        self.assertEqual(picking.x_dispatch_id, dispatch)
        self.assertEqual(dispatch.state, 'delivered')
        self.assertEqual(dispatch.picking_id, picking)
        self.assertNotIn(dispatch, self.Dispatch._water_open_for_driver(self.driver))

    def test_the_driver_client_list_does_not_hold_a_dispatched_client(self):
        """The dispatcher choosing the client IS the authority the driver's
        own list stands in for."""
        self.driver.sudo().x_water_client_ids = [(6, 0, self.multi_customer.ids)]
        capture = self._confirm(self._dispatch())
        self.assertEqual(capture.state, 'issued')

    def test_only_its_driver_can_confirm_it(self):
        dispatch = self._dispatch()
        capture = self._confirm(dispatch, user=self.other_driver_user)
        self.assertEqual(capture.state, 'failed')
        self.assertFalse(capture.picking_id)
        self.assertEqual(dispatch.state, 'dispatched')
        self.assertFalse(dispatch.capture_id)

    def test_one_dispatch_makes_one_note(self):
        dispatch = self._dispatch()
        first = self._confirm(dispatch, ref='a')
        second = self._confirm(dispatch, ref='b')
        self.assertEqual(first.state, 'issued')
        self.assertEqual(second.state, 'failed')
        self.assertFalse(second.picking_id)
        self.assertEqual(dispatch.capture_id, first)

    def test_out_of_range_is_held_and_a_rejection_reopens_the_order(self):
        self._enforce_here()
        dispatch = self._dispatch()
        capture = self._confirm(dispatch, gps_latitude=FAR[0], gps_longitude=FAR[1])
        self.assertEqual(capture.state, 'held')
        self.assertTrue(dispatch.note_held)

        capture.with_user(self.dispatcher_user).write({'review_note': 'Not there'})
        capture.with_user(self.dispatcher_user).action_reject()
        self.assertEqual(dispatch.state, 'dispatched')
        self.assertFalse(dispatch.picking_id)
        self.assertIn(dispatch, self.Dispatch._water_open_for_driver(self.driver))

        again = self._confirm(dispatch, ref='again',
                              gps_latitude=HERE[0], gps_longitude=HERE[1])
        self.assertEqual(again.state, 'issued')
        self.assertEqual(dispatch.state, 'delivered')

    def test_cancelled_dispatch_cannot_be_confirmed(self):
        dispatch = self._dispatch()
        dispatch.with_user(self.dispatch_user).action_cancel()
        capture = self._confirm(dispatch)
        self.assertEqual(capture.state, 'failed')
        self.assertFalse(capture.picking_id)


@tagged('post_install', '-at_install')
class TestDispatchOverTheAppRoutes(HttpCase):
    """The phone's own routes, over HTTP: the reference carries the mode and
    the open dispatch, and a sync naming it issues the note and takes it off
    the list."""

    def setUp(self):
        super().setUp()
        env = self.env
        self.picking_type = env.ref('KSW_water_delivery.picking_type_water_out')
        self.picking_type.write({
            'x_branch_code': '172', 'x_signature_required': False,
            'x_location_rule': 'capture', 'x_issue_mode': 'dispatch',
        })
        self.driver_user = env['res.users'].create({
            'name': 'Dispatch App Driver', 'login': 'dsp.driver@ksw.test',
            'password': 'dsp.driver@ksw.test',
            'group_ids': [(6, 0, [env.ref('KSW_water_delivery.group_water_driver').id])],
        })
        driver = env['hr.employee'].create({
            'name': 'Dispatch App Driver', 'user_id': self.driver_user.id})
        env['ksw.fleet.vehicle'].create({
            'name': 'T-DSP', 'client_id': env.company.partner_id.id,
            'vehicle_type': 'trailer', 'plate_number': 'DSP 1',
            'driver_id': driver.id, 'state': 'confirmed', 'x_capacity_m3': 32.0,
            'x_picking_type_id': self.picking_type.id,
        })
        product = env['product.product'].create({
            'name': 'Dispatch Test Water', 'type': 'consu', 'is_storable': False,
            'invoice_policy': 'delivery', 'sale_ok': True,
        })
        self.client = env['res.partner'].create({'name': 'Dispatch Client', 'customer_rank': 1})
        env['product.pricelist.item'].create({
            'x_client_id': self.client.id, 'product_id': product.id, 'fixed_price': 50.0})
        env['ksw.water.client.branch'].create({
            'partner_id': self.client.id, 'branch_code': '172'})
        self.dispatch = env['ksw.water.dispatch'].create({
            'picking_type_id': self.picking_type.id, 'partner_id': self.client.id,
            'entered_qty': 2.0, 'driver_user_id': self.driver_user.id,
        })
        self.dispatch.action_dispatch()

    def _rpc(self, route, params=None):
        response = self.url_open(route, data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call', 'params': params or {},
        }), headers={'Content-Type': 'application/json'})
        return response.json()

    def test_the_phone_sees_and_confirms_its_dispatch(self):
        self.authenticate('dsp.driver@ksw.test', 'dsp.driver@ksw.test')
        reference = self._rpc('/water/reference')['result']
        self.assertEqual(reference['rules']['issue_mode'], 'dispatch')
        self.assertEqual([d['id'] for d in reference['dispatches']], [self.dispatch.id])
        self.assertEqual(reference['dispatches'][0]['client']['name'], 'Dispatch Client')

        now = fields.Datetime.to_string(fields.Datetime.now())
        result = self._rpc('/water/sync', {'captures': [{
            'uuid': 'dsp-http-1', 'dispatch_id': self.dispatch.id,
            'partner_id': self.client.id, 'product_id': 0, 'entered_qty': 2.0,
            'entered_uom': 'product', 'captured_at': now, 'sent_at': now,
            'sent_live': True,
        }]})['result']['results'][0]
        self.assertEqual(result['status'], 'issued', result)

        self.assertEqual(self.dispatch.state, 'delivered')
        reference = self._rpc('/water/reference')['result']
        self.assertEqual(reference['dispatches'], [])
