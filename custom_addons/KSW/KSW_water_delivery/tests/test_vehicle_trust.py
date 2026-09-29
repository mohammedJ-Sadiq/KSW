# -*- coding: utf-8 -*-
"""The tanker is never the phone's or the driver's choice.

Audit 2026-09-28: `/water/sync` trusted the payload `vehicle_id`, and the
tanker sets both the trip volume (trips x capacity) and the branch rules.
A driver of a small truck could send a 32 m3 trailer and enter "1 trip".
"""
from odoo.exceptions import UserError
from odoo.tests import tagged

from .test_offline_capture import OfflineCaptureCommon


@tagged('post_install', '-at_install')
class TestVehicleTrust(OfflineCaptureCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other_vehicle = cls.env['ksw.fleet.vehicle'].create({
            'name': 'T-200', 'client_id': cls.company.partner_id.id,
            'vehicle_type': 'trailer', 'plate_number': 'XYZ 9876',
            'driver_id': cls.other_driver.id, 'state': 'confirmed',
            'x_capacity_m3': 32.0,
            'x_picking_type_id': cls.picking_type.id,
        })

    def test_capture_on_someone_elses_tanker_is_held(self):
        capture = self.Capture.with_user(self.driver_user)._receive(
            self._payload(ref='veh1', vehicle_id=self.other_vehicle.id,
                          entered_uom='trip', entered_qty=1.0),
            self.driver)
        self.assertEqual(capture.state, 'held')
        self.assertIn('not assigned', capture.hold_reason)
        self.assertEqual(capture.picking_id.state != 'done', True,
                         'a held note is not delivered, so not billable')

    def test_capture_on_own_tanker_issues(self):
        capture = self.Capture.with_user(self.driver_user)._receive(
            self._payload(ref='veh2'), self.driver)
        self.assertEqual(capture.state, 'issued')

    def test_live_issue_on_someone_elses_tanker_refused(self):
        with self.assertRaises(UserError):
            self.env['stock.picking'].with_user(self.driver_user).sudo(
            )._issue_water_note(dict(
                self._payload(ref='veh3', vehicle_id=self.other_vehicle.id),
                driver_id=self.driver.id), location_mode='raise')

    def test_live_issue_in_someone_elses_name_refused(self):
        with self.assertRaises(UserError):
            self.env['stock.picking'].with_user(self.driver_user).sudo(
            )._issue_water_note(dict(
                self._payload(ref='veh4', vehicle_id=self.other_vehicle.id),
                driver_id=self.other_driver.id), location_mode='raise')

    def test_dispatcher_may_use_any_tanker(self):
        picking = self.env['stock.picking'].with_user(
            self.dispatcher_user).sudo()._issue_water_note(dict(
                self._payload(ref='veh5', vehicle_id=self.other_vehicle.id),
                driver_id=self.other_driver.id), location_mode='raise')
        self.assertFalse(picking.x_location_exception)
