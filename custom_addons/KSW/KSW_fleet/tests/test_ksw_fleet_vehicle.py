from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestKswFleetVehicle(TransactionCase):

    def test_display_name_with_model(self):
        vehicle = self.env['ksw.fleet.vehicle'].create({
            'name': '152',
            'vehicle_model': 'سياب',
        })
        self.assertEqual(vehicle.display_name, '152 — سياب')

    def test_display_name_without_model(self):
        vehicle = self.env['ksw.fleet.vehicle'].create({'name': '161'})
        self.assertEqual(vehicle.display_name, '161')

    def test_duplicate_name_rejected(self):
        self.env['ksw.fleet.vehicle'].create({'name': '200'})
        with self.assertRaises(Exception):
            with self.env.cr.savepoint():
                self.env['ksw.fleet.vehicle'].create({'name': '200'})
                self.env.flush_all()
