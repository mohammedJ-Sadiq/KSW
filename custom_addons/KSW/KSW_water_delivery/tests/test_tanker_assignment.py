"""Water Delivery > Tankers: dispatch assigns drivers, in the water app.

Driven as a dispatcher with NO HR rights, because that is who uses it -- an
admin-run test would pass through every gate this screen depends on.
"""
from odoo.exceptions import UserError
from odoo.tests import tagged

from .test_offline_capture import OfflineCaptureCommon


@tagged('post_install', '-at_install')
class TestTankerAssignment(OfflineCaptureCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        assert not cls.dispatcher_user.has_group('hr.group_hr_user')
        Vehicle = cls.env['ksw.fleet.vehicle']
        cls.spare = Vehicle.create({
            'name': 'T-500', 'client_id': cls.company.partner_id.id,
            'vehicle_type': 'trailer', 'plate_number': 'SPR 5000',
            'state': 'confirmed', 'x_capacity_m3': 32.0,
            'x_picking_type_id': cls.picking_type.id,
        })
        cls.clients_truck = Vehicle.create({
            'name': 'WS-1', 'client_id': cls.customer.id, 'state': 'confirmed',
        })
        cls.no_login = cls.env['hr.employee'].create({
            'name': 'Yard Hand', 'parent_id': cls.driver.parent_id.id})

    def _screen(self):
        action = self.env.ref('KSW_water_delivery.action_water_tankers')
        Vehicle = self.env['ksw.fleet.vehicle'].with_user(self.dispatcher_user)
        return Vehicle.with_context(**dict(eval(action.context))), eval(action.domain)

    def test_the_screen_loads_for_a_dispatcher(self):
        """What the list actually reads, including the two pickers' labels."""
        Vehicle, domain = self._screen()
        result = Vehicle.web_search_read(domain, {
            'name': {}, 'vehicle_type': {}, 'plate_number': {},
            'driver_id': {'fields': {'display_name': {}}},
            'x_picking_type_id': {'fields': {'display_name': {}}},
            'x_capacity_m3': {},
        })
        ids = [r['id'] for r in result['records']]
        self.assertIn(self.vehicle.id, ids)
        self.assertNotIn(self.clients_truck.id, ids,
                         "a workshop client's vehicle is not one of our tankers")

    def test_the_pickers_work_for_a_dispatcher(self):
        employees = self.env['hr.employee'].with_user(self.dispatcher_user).name_search('Driver')
        self.assertTrue(employees)
        types = self.env['stock.picking.type'].with_user(self.dispatcher_user).name_search(
            '', [('x_is_water_delivery', '=', True)])
        self.assertTrue(types)

    def test_assigning_a_driver_moves_him(self):
        """One tanker per driver, or the app refuses to guess -- so assigning
        here is a move, not a second assignment."""
        Vehicle, _domain = self._screen()
        Vehicle.browse(self.spare.id).write({'driver_id': self.driver.id})

        self.assertEqual(self.spare.driver_id, self.driver)
        self.assertFalse(self.vehicle.driver_id)
        vehicle, problem = self.env['stock.picking']._water_assigned_vehicle(self.driver)
        self.assertEqual(vehicle, self.spare)
        self.assertIsNone(problem)

    def test_outside_this_screen_nothing_is_moved(self):
        """In Workshop, 'default driver' is information; HR may put one name
        on several vehicles and this must not undo it."""
        self.spare.write({'driver_id': self.driver.id})
        self.assertEqual(self.vehicle.driver_id, self.driver)

    def test_a_dispatcher_changes_only_dispatch_fields(self):
        Vehicle, _domain = self._screen()
        tanker = Vehicle.browse(self.spare.id)
        tanker.write({'x_capacity_m3': 30.0, 'x_picking_type_id': self.picking_type.id})
        self.assertEqual(self.spare.x_capacity_m3, 30.0)
        with self.assertRaisesRegex(UserError, 'only change the driver'):
            tanker.write({'plate_number': 'HACK 1'})

    def test_a_driver_without_a_login_is_refused(self):
        Vehicle, _domain = self._screen()
        with self.assertRaisesRegex(UserError, 'no Water Delivery driver login'):
            Vehicle.browse(self.spare.id).write({'driver_id': self.no_login.id})

    def test_a_driver_cannot_assign_tankers(self):
        with self.assertRaises(Exception):
            self.spare.with_user(self.driver_user).write({'driver_id': self.driver.id})
