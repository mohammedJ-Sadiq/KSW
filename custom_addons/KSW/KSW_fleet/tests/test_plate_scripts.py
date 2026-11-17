from odoo.tests import TransactionCase, tagged

from odoo.addons.KSW_fleet.models.plates import plate_arabic, plate_english, plate_key


@tagged('post_install', '-at_install')
class TestPlateBothScripts(TransactionCase):

    def test_conversion(self):
        # The English letters sit under their Arabic ones: reversed when read.
        self.assertEqual(plate_english('أ ص ر 8935'), 'RXA 8935')
        self.assertEqual(plate_arabic('ZTB-8617'), 'ب ط م 8617')
        self.assertEqual(plate_arabic('أ ص ر ٨٩٣٥'), 'أ ص ر 8935')
        self.assertEqual(plate_key('RXA 8935'), plate_key('أ ص ر 8935'))
        # Not a plate: left alone.
        self.assertEqual(plate_arabic('TEST-120'), '')

    def test_vehicle_stores_both(self):
        Vehicle = self.env['ksw.fleet.vehicle']
        a = Vehicle.create({'name': 'PLT-1', 'plate_number': 'أ ط د 4253'})
        self.assertEqual((a.plate_number, a.x_plate_en), ('أ ط د 4253', 'DTA 4253'))
        b = Vehicle.create({'name': 'PLT-2', 'x_plate_en': 'ZTB 8617'})
        self.assertEqual((b.plate_number, b.x_plate_en), ('ب ط م 8617', 'ZTB 8617'))
        b.plate_number = 'JXB-8856'
        self.assertEqual((b.plate_number, b.x_plate_en), ('ب ص ح 8856', 'JXB 8856'))
        c = Vehicle.create({'name': 'PLT-3', 'plate_number': 'TEST-120'})
        self.assertEqual((c.plate_number, c.x_plate_en), ('TEST-120', False))
