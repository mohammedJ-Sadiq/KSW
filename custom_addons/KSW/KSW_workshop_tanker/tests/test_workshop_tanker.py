import datetime
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

FLEET = 'odoo.addons.KSW_workshop_tanker.models.fleet_vehicle.KswFleetVehicle'


@tagged('post_install', '-at_install')
class TestWorkshopTanker(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)
        Employee = cls.env['hr.employee']

        def _user(login, groups=('base.group_user', 'KSW_workshop.group_workshop_requester')):
            return Users.create({
                'name': login, 'login': login, 'email': f'{login}@wst.test',
                'group_ids': [(6, 0, [cls.env.ref(g).id for g in groups])]})

        cls.sup_user = _user('wst_supervisor')
        cls.supervisor = Employee.create({'name': 'WST Supervisor', 'user_id': cls.sup_user.id})
        cls.loner_user = _user('wst_loner')
        cls.loner = Employee.create({'name': 'WST Loner', 'user_id': cls.loner_user.id})
        cls.mgr_user = _user('wst_manager', ('base.group_user', 'KSW_workshop.group_workshop_manager'))
        Employee.create({'name': 'WST Manager', 'user_id': cls.mgr_user.id})
        cls.admin_user = _user('wst_admin', ('base.group_user', 'base.group_system'))

        cls.driver_a = Employee.create({
            'name': 'WST Driver A', 'parent_id': cls.supervisor.id,
            'x_bas_driver_cost_center': 'WST DRIVER A9001'})
        cls.driver_b = Employee.create({'name': 'WST Driver B', 'x_employee_no': '9002'})
        cls.outsider = Employee.create({'name': 'WST Outsider'})

        Vehicle = cls.env['ksw.fleet.vehicle']
        cls.in_group = Vehicle.create({'name': 'IS9101', 'vehicle_type': 'isuzu'})
        cls.team_truck = Vehicle.create({'name': 'IS9102', 'vehicle_type': 'isuzu',
                                         'driver_id': cls.driver_a.id})
        cls.other_truck = Vehicle.create({'name': 'IS9103', 'vehicle_type': 'isuzu',
                                          'driver_id': cls.outsider.id})
        cls.trailer = Vehicle.create({'name': 'T9104', 'vehicle_type': 'trailer'})
        cls.client = cls.env['res.partner'].create({'name': 'WST Client', 'customer_rank': 1})
        cls.client_truck = Vehicle.create({'name': 'IS9105', 'vehicle_type': 'isuzu',
                                           'client_id': cls.client.id})
        cls.env['ksw.water.tanker.group'].create({
            'name': 'WST Group', 'vehicle_ids': [(6, 0, cls.in_group.ids)],
            'supervisor_ids': [(6, 0, cls.supervisor.ids)]})
        # The fill-in is switched off in production (Oct 2026); these tests
        # keep its behaviour covered for when it is switched back on.
        cls.env['ir.config_parameter'].sudo().set_param('KSW_workshop.driver_follows_vehicle', '1')

    def _pickable(self, user):
        return self.env['ksw.fleet.vehicle'].with_user(user).search(
            [('x_workshop_in_scope', '=', True), ('name', 'like', 'IS91')])

    # ------------------------------------------------------------------ scope
    def test_supervisor_sees_his_groups_and_his_teams_trucks(self):
        picked = self._pickable(self.sup_user)
        self.assertIn(self.in_group, picked)
        self.assertIn(self.team_truck, picked)
        self.assertNotIn(self.other_truck, picked)
        # A client's truck is the client's, never narrowed.
        self.assertIn(self.client_truck, picked)

    def test_bas_driver_wins_over_default_driver_for_scope(self):
        self.team_truck.x_bas_driver_id = self.outsider
        self.other_truck.x_bas_driver_id = self.driver_a
        picked = self._pickable(self.sup_user)
        self.assertNotIn(self.team_truck, picked)
        self.assertIn(self.other_truck, picked)

    def test_workshop_manager_and_unscoped_requester_keep_full_list(self):
        for user in (self.mgr_user, self.loner_user):
            self.assertIn(self.other_truck, self._pickable(user))

    def test_request_outside_scope_is_refused(self):
        with self.assertRaises(UserError):
            self.env['ksw.workshop.request'].with_user(self.sup_user).create({
                'vehicle_type': 'isuzu', 'vehicle_id': self.other_truck.id,
                'driver_id': self.outsider.id, 'description': 'Oil change'})

    # ---------------------------------------------------------- driver check
    def test_attached_driver_filled_and_mismatch_flagged(self):
        Request = self.env['ksw.workshop.request'].with_user(self.sup_user)
        same = Request.create({'vehicle_type': 'isuzu', 'vehicle_id': self.team_truck.id,
                               'description': 'Oil change'})
        self.assertEqual(same.driver_id, self.driver_a)
        self.assertEqual(same.x_attached_driver_id, self.driver_a)
        self.assertFalse(same.x_driver_mismatch)

        other = Request.create({'vehicle_type': 'isuzu', 'vehicle_id': self.team_truck.id,
                                'driver_id': self.supervisor.id, 'description': 'Brakes'})
        self.assertTrue(other.x_driver_mismatch)
        self.assertEqual(other.x_attached_driver_id, self.driver_a)

    def test_switched_off_the_vehicle_fills_in_no_driver(self):
        self.env['ir.config_parameter'].sudo().set_param('KSW_workshop.driver_follows_vehicle', '0')
        request = self.env['ksw.workshop.request'].with_user(self.sup_user).create({
            'vehicle_type': 'isuzu', 'vehicle_id': self.team_truck.id,
            'driver_id': self.supervisor.id, 'description': 'Oil change'})
        self.assertEqual(request.driver_id, self.supervisor)
        self.assertFalse(request.x_attached_driver_id)
        self.assertFalse(request.x_driver_mismatch)
        form_request = self.env['ksw.workshop.request'].with_user(self.sup_user).new(
            {'vehicle_type': 'isuzu', 'vehicle_id': self.team_truck.id})
        form_request._onchange_vehicle_id()
        self.assertFalse(form_request.driver_id)
        # The scope still follows who drives what: only the fill-in stops.
        self.assertIn(self.team_truck, self.env['ksw.fleet.vehicle'].with_user(self.sup_user).search(
            [('x_workshop_in_scope', '=', True)]))

    def test_attached_driver_is_a_snapshot(self):
        request = self.env['ksw.workshop.request'].with_user(self.sup_user).create({
            'vehicle_type': 'isuzu', 'vehicle_id': self.team_truck.id, 'description': 'Tyres'})
        self.team_truck.x_bas_driver_id = self.driver_b
        request.invalidate_recordset()
        self.assertEqual(request.x_attached_driver_id, self.driver_a)
        self.assertFalse(request.x_driver_mismatch)
        # The client cannot set the snapshot itself.
        request.with_user(self.sup_user).write({'x_attached_driver_id': self.outsider.id})
        self.assertEqual(request.x_attached_driver_id, self.driver_a)

    # ------------------------------------------------------------------- BAS
    def _sync(self, rows, user=None):
        when = datetime.datetime(2026, 10, 12, 10, 0)
        rows = [dict(cc=cc, driver_cc=d, loads=n, last_date=when - datetime.timedelta(days=age))
                for cc, d, n, age in rows]
        with patch(FLEET + '._bas_fetch_vehicle_drivers', return_value=rows):
            return self.env['ksw.fleet.vehicle'].with_user(user or self.admin_user) \
                .action_update_drivers_from_bas()

    def test_bas_sync_picks_dominant_driver_and_matches(self):
        result = self._sync([
            ('ايسوزو 9101', 'WST\xa0Driver  A9001', 40, 1),   # label match, NBSP
            ('ايسوزو 9101', 'Someone Else 77', 1, 0),          # one-day substitute
            ('T9104', 'Bas Spelling 9002', 12, 2),             # employee-no. fallback
            ('ايسوزو9103', 'customer car 1001', 99, 0),        # not a person
            ('ايسوزو9103', 'Nobody Known 5555', 3, 0),         # unplaced
            ('ثلج 1012', 'WST DRIVER A9001', 50, 0),          # not a fleet truck
            ('ايسوزو 9199', 'WST DRIVER A9001', 5, 0),        # not in the fleet
        ])
        self.assertEqual(self.in_group.x_bas_driver_id, self.driver_a)
        self.assertEqual(self.in_group.x_bas_driver_date, datetime.date(2026, 10, 11))
        self.assertEqual(self.trailer.x_bas_driver_id, self.driver_b)
        self.assertFalse(self.other_truck.x_bas_driver_id)
        self.assertEqual(self.other_truck.x_bas_driver_label, 'Nobody Known 5555')
        self.assertEqual(self.other_truck.x_attached_driver_id, self.outsider)
        self.assertFalse(self.client_truck.x_bas_driver_label)
        self.assertIn('9199', result['params']['message'])
        self.assertIn('Nobody Known 5555', result['params']['message'])

    def test_bas_sync_is_admin_only(self):
        with self.assertRaises(UserError):
            self._sync([], user=self.mgr_user)

    # ---------------------------------------------------------------- submit
    def test_submit_button_only_on_a_new_request(self):
        arch = self.env['ksw.workshop.request'].with_user(self.sup_user).get_views(
            [(False, 'form')])['views']['form']['arch']
        self.assertIn('name="action_submit"', arch)
        request = self.env['ksw.workshop.request'].with_user(self.sup_user).create({
            'vehicle_type': 'isuzu', 'vehicle_id': self.team_truck.id, 'description': 'Oil change'})
        result = request.action_submit()
        self.assertEqual(result['params']['type'], 'success')
        self.assertIn(request.name, result['params']['message'])
