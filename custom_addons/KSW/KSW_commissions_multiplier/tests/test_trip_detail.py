"""Trip Details: one row per BAS load, weighted as the importer weighs it."""
from datetime import date
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from ..models.trip_detail import KswPayBatchTripLines

CODE = '1203ZZ950'


def _line(day, code=CODE, amount=0.0, cash=False, band=0.0, factore=2.0, phrdis=0.0):
    return {'driver_cc': 'TD DRIVER 1', 'truck': 'T9', 'day': day, 'document': '120/%d' % day.day,
            'code': code, 'customer': 'BAS name', 'amount': amount, 'factore': factore,
            'phrdis': phrdis, 'is_cash': cash, 'band': band}


@tagged('post_install', '-at_install')
class TestTripDetail(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.dept = env['hr.department'].create({'name': 'TD Drivers'})
        cls.driver = env['hr.employee'].sudo().create({
            'name': 'TD Driver', 'department_id': cls.dept.id,
            'x_bas_driver_cost_center': 'TD DRIVER 1'})
        customer = env['ksw.bas.customer'].create({'bas_code': CODE, 'name_ar': 'عميل'})
        cls.client = env['ksw.client.multiplier'].create({'customer_id': customer.id})
        Rate = env['ksw.client.multiplier.rate']
        Rate.create({'client_id': cls.client.id, 'multiplier': 2.0, 'source': 'opening'})
        Rate.create({'client_id': cls.client.id, 'multiplier': 1.25,
                     'date_from': date(2026, 10, 20)})
        cls.Batch = env['ksw.pay.batch']

    def test_each_basis_is_the_importers(self):
        lines = self.Batch._weigh_trip_lines([
            _line(date(2026, 9, 2)),                               # before «رد الفاتورة»
            _line(date(2026, 9, 10), phrdis=1.5),                  # inside the window
            _line(date(2026, 9, 11), phrdis=0.0),                  # window, unrecorded
            _line(date(2026, 10, 5)),                              # Odoo opening value
            _line(date(2026, 10, 25)),                             # Odoo dated change
            _line(date(2026, 10, 25), code='1203NOPE'),            # Odoo has no value
            _line(date(2026, 10, 25), code='1201020015', cash=True, amount=800, band=3.0),
        ])
        got = [(l['basis'], l['weight']) for l in lines]
        self.assertEqual(got, [('bas', 2.0), ('invoice', 1.5), ('none', 0.0), ('odoo', 2.0),
                               ('odoo', 1.25), ('none', 0.0), ('cash', 3.0)])
        self.assertEqual(lines[3]['source'], 'Opening value')
        self.assertEqual(lines[3]['customer'], self.client.name)

    def test_the_dialog_adds_up_to_the_line(self):
        batch = self.Batch.sudo().create({
            'component_id': self.env.ref('KSW_commissions.pay_component_driver_trips').id,
            'period': date(2026, 10, 1), 'department_id': self.dept.id})
        entry = self.env['ksw.pay.entry'].sudo().create({
            'batch_id': batch.id, 'employee_id': self.driver.id, 'quantity': 5.25,
            'quantity_ref': 3, 'threshold_qty': 0})
        rows = [_line(date(2026, 10, 5)), _line(date(2026, 10, 25)),
                _line(date(2026, 10, 25)), _line(date(2026, 10, 26), code='OTHER CC')]
        rows[3]['driver_cc'] = 'SOMEONE ELSE'
        with patch.object(KswPayBatchTripLines, '_bas_trip_lines', lambda self, f, t: [dict(r) for r in rows]):
            action = entry.action_trip_details()
        detail = self.env['ksw.pay.trip.detail'].browse(action['res_id'])
        self.assertEqual(detail.loads, 3)                 # the other driver's load is not his
        self.assertAlmostEqual(detail.weighted, 2.0 + 1.25 + 1.25)
        self.assertAlmostEqual(detail.difference, 4.5 - 5.25)
        self.assertIn('Odoo', detail.summary)
