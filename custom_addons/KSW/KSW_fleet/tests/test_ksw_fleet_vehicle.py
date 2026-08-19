from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestKswFleetVehicle(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _mkuser(name, login, group_xmlids=('base.group_user',)):
            return cls.env['res.users'].create({
                'name': name,
                'login': login,
                'email': f'{login}@fleettest.test',
                'group_ids': [(6, 0, [cls.env.ref(xmlid).id for xmlid in group_xmlids])],
            })

        cls.user_employee = _mkuser('Fleet Employee', 'fleettest_employee')
        cls.user_hr = _mkuser(
            'Fleet HR', 'fleettest_hr', group_xmlids=('base.group_user', 'hr.group_hr_user'),
        )
        cls.other_client = cls.env['res.partner'].create({'name': 'Fleet Test Other Client'})

    # Fixture vehicle names are prefixed "FLEETTEST-" rather than bare
    # numbers: the history-imported fleet already contains real vehicles
    # named with bare numbers (e.g. "200", "201" — the "ambiguous/unlisted
    # code, use the bare number" fallback from import_history.py), and with
    # client_id+name now unique together, a bare-number fixture can collide
    # with real data sharing the same (company) client.

    def test_display_name_with_brand_model(self):
        vehicle = self.env['ksw.fleet.vehicle'].create({
            'name': 'FLEETTEST-1', 'brand': 'Isuzu', 'model': 'NPR 75',
        })
        self.assertEqual(vehicle.display_name, 'FLEETTEST-1 — Isuzu NPR 75')

    def test_display_name_without_brand_model(self):
        vehicle = self.env['ksw.fleet.vehicle'].create({'name': 'FLEETTEST-2'})
        self.assertEqual(vehicle.display_name, 'FLEETTEST-2')

    def test_duplicate_name_rejected(self):
        self.env['ksw.fleet.vehicle'].create({'name': 'FLEETTEST-3'})
        with self.assertRaises(Exception):
            with self.env.cr.savepoint():
                self.env['ksw.fleet.vehicle'].create({'name': 'FLEETTEST-3'})
                self.env.flush_all()

    def test_duplicate_name_allowed_across_different_clients(self):
        self.env['ksw.fleet.vehicle'].create({'name': 'FLEETTEST-4'})
        other = self.env['ksw.fleet.vehicle'].create({
            'name': 'FLEETTEST-4', 'client_id': self.other_client.id,
        })
        self.assertEqual(other.name, 'FLEETTEST-4')

    def test_client_defaults_to_company(self):
        vehicle = self.env['ksw.fleet.vehicle'].create({'name': 'FLEETTEST-5'})
        self.assertEqual(vehicle.client_id, self.env.company.partner_id)

    def test_state_defaults_confirmed_for_manager(self):
        vehicle = self.env['ksw.fleet.vehicle'].with_user(self.user_hr).create({'name': 'FLEETTEST-6'})
        self.assertEqual(vehicle.state, 'confirmed')

    def test_state_defaults_draft_for_regular_employee(self):
        vehicle = self.env['ksw.fleet.vehicle'].with_user(self.user_employee).create({'name': 'FLEETTEST-7'})
        self.assertEqual(vehicle.state, 'draft')

    def test_action_confirm_requires_manager(self):
        vehicle = self.env['ksw.fleet.vehicle'].with_user(self.user_employee).create({'name': 'FLEETTEST-8'})
        with self.assertRaises(UserError):
            vehicle.with_user(self.user_employee).action_confirm()

    def test_action_confirm_by_manager(self):
        vehicle = self.env['ksw.fleet.vehicle'].with_user(self.user_employee).create({'name': 'FLEETTEST-9'})
        vehicle.with_user(self.user_hr).action_confirm()
        self.assertEqual(vehicle.state, 'confirmed')

    def test_action_confirm_twice_raises(self):
        vehicle = self.env['ksw.fleet.vehicle'].create({'name': 'FLEETTEST-10'})
        with self.assertRaises(UserError):
            vehicle.action_confirm()

    def test_driver_name_synced_from_driver_id(self):
        employee = self.env['hr.employee'].create({'name': 'Fleet Test Driver'})
        vehicle = self.env['ksw.fleet.vehicle'].create({'name': 'FLEETTEST-11', 'driver_id': employee.id})
        self.assertEqual(vehicle.driver_name, 'Fleet Test Driver')

    def test_driver_name_manual_override_survives_unrelated_write(self):
        vehicle = self.env['ksw.fleet.vehicle'].create({'name': 'FLEETTEST-12', 'driver_name': 'Typed Driver'})
        vehicle.write({'plate_number': 'ABC-123'})
        self.assertEqual(vehicle.driver_name, 'Typed Driver')
