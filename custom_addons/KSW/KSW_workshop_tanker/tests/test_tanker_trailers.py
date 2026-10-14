from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

FLEET = 'odoo.addons.KSW_workshop_tanker.models.fleet_vehicle.KswFleetVehicle'


@tagged('post_install', '-at_install')
class TestTankerTrailers(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)
        cls.sup_user = Users.create({
            'name': 'wtd_sup', 'login': 'wtd_sup',
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id,
                                  cls.env.ref('KSW_workshop.group_workshop_requester').id])]})
        supervisor = cls.env['hr.employee'].create({'name': 'WTD Sup', 'user_id': cls.sup_user.id})
        cls.admin_user = Users.create({
            'name': 'wtd_admin', 'login': 'wtd_admin',
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id, cls.env.ref('base.group_system').id])]})
        Vehicle = cls.env['ksw.fleet.vehicle']
        cls.head = Vehicle.create({'name': 'T9301', 'vehicle_type': 'trailer'})
        cls.foreign_head = Vehicle.create({'name': 'T9302', 'vehicle_type': 'trailer'})
        cls.trailer = Vehicle.create({'name': 'تيدر 9303', 'vehicle_type': 'tank'})
        cls.env['ksw.water.tanker.group'].create({
            'name': 'WTD Group', 'vehicle_ids': [(6, 0, cls.head.ids)],
            'supervisor_ids': [(6, 0, supervisor.ids)]})

    def _import(self, rows, user=None):
        with patch(FLEET + '._bas_fetch_tanker_trailers', return_value=rows):
            return self.env['ksw.fleet.vehicle'].with_user(user or self.admin_user) \
                .action_import_tanker_trailers_from_bas()

    def test_every_requester_may_pick_any_tanker_trailer(self):
        picked = self.env['ksw.fleet.vehicle'].with_user(self.sup_user).search(
            [('x_workshop_in_scope', '=', True), ('name', 'in', ['T9301', 'T9302', 'تيدر 9303'])])
        self.assertEqual(picked, self.head | self.trailer)
        outside = self.env['ksw.fleet.vehicle'].with_user(self.sup_user).search(
            [('x_workshop_in_scope', '=', False), ('name', 'in', ['T9301', 'T9302', 'تيدر 9303'])])
        self.assertEqual(outside, self.foreign_head)

    def test_import_creates_missing_tanker_trailers_once(self):
        rows = [{'code': '1449001', 'name': 'تيدر رقم 9304'},
                {'code': '1449002', 'name': 'تيدررقم 9305'},
                {'code': '1449003', 'name': 'تيدر رقم 9303'},     # already in the fleet
                {'code': '1449004', 'name': 'التيادر T'}]          # group node, no number
        self._import(rows)
        Vehicle = self.env['ksw.fleet.vehicle']
        new = Vehicle.search([('name', 'in', ['تيدر 9304', 'تيدر 9305'])])
        self.assertEqual(len(new), 2)
        self.assertEqual(set(new.mapped('vehicle_type')), {'tank'})
        self.assertEqual(set(new.mapped('state')), {'confirmed'})
        self.assertEqual(Vehicle.search_count([('name', 'like', 'تيدر 9303')]), 1)
        self._import(rows)
        self.assertEqual(Vehicle.search_count([('name', 'like', 'تيدر 930')]), 3)

    def test_only_an_administrator_imports(self):
        with self.assertRaises(UserError):
            self._import([], user=self.sup_user)
