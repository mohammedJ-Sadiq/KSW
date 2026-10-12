"""Water Delivery > Tankers: dispatch sets the driver's client list.

Driven as a dispatcher with NO HR rights -- the employee form, where the list
used to be the only way in, is exactly what that user cannot open.
"""
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .test_offline_capture import OfflineCaptureCommon


@tagged('post_install', '-at_install')
class TestTankerDriverClients(OfflineCaptureCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        assert not cls.dispatcher_user.has_group('hr.group_hr_user')
        cls.other_client = cls.env['res.partner'].create({
            'name': 'Second Site', 'customer_rank': 1})
        cls.spare = cls.env['ksw.fleet.vehicle'].create({
            'name': 'T-600', 'client_id': cls.company.partner_id.id,
            'vehicle_type': 'trailer', 'state': 'confirmed',
        })
        cls.tanker_group.vehicle_ids = [(4, cls.spare.id)]

    def _screen(self):
        action = self.env.ref('KSW_water_delivery.action_water_tankers')
        Vehicle = self.env['ksw.fleet.vehicle'].with_user(self.dispatcher_user)
        return Vehicle.with_context(**dict(eval(action.context))), eval(action.domain)

    def test_the_column_loads_for_a_dispatcher(self):
        self.driver.sudo().x_water_client_ids = [(6, 0, self.customer.ids)]
        Vehicle, domain = self._screen()
        result = Vehicle.web_search_read(domain, {
            'driver_id': {'fields': {'display_name': {}}},
            'x_driver_client_ids': {'fields': {'display_name': {}}},
        })
        row = next(r for r in result['records'] if r['id'] == self.vehicle.id)
        self.assertEqual([c['id'] for c in row['x_driver_client_ids']], self.customer.ids)

    def test_a_dispatcher_sets_the_list_and_it_lands_on_the_driver(self):
        Vehicle, _domain = self._screen()
        Vehicle.browse(self.vehicle.id).write({
            'x_driver_client_ids': [(6, 0, [self.customer.id, self.other_client.id])]})
        self.assertEqual(self.driver.sudo().x_water_client_ids,
                         self.customer | self.other_client)

    def test_the_list_narrows_what_the_app_offers(self):
        Vehicle, _domain = self._screen()
        Vehicle.browse(self.vehicle.id).write({
            'x_driver_client_ids': [(6, 0, self.customer.ids)]})
        normal, _rated = self.env['stock.picking']._water_client_scope(
            self.driver, self.picking_type)
        self.assertEqual(normal, normal & self.customer)

    def test_clearing_the_list_widens_him_back_to_the_branch(self):
        self.driver.sudo().x_water_client_ids = [(6, 0, self.customer.ids)]
        Vehicle, _domain = self._screen()
        Vehicle.browse(self.vehicle.id).write({'x_driver_client_ids': [(5, 0, 0)]})
        self.assertFalse(self.driver.sudo().x_water_client_ids)

    def test_the_list_follows_the_driver_to_another_tanker(self):
        self.driver.sudo().x_water_client_ids = [(6, 0, self.customer.ids)]
        Vehicle, _domain = self._screen()
        Vehicle.browse(self.spare.id).write({'driver_id': self.driver.id})
        self.assertEqual(self.spare.x_driver_client_ids, self.customer)
        self.assertFalse(self.vehicle.x_driver_client_ids)

    def test_new_driver_and_his_clients_in_one_save(self):
        """The list row sends both at once; the clients must land on the
        driver being assigned, not on the tanker's previous one."""
        Vehicle, _domain = self._screen()
        Vehicle.browse(self.spare.id).write({
            'driver_id': self.driver.id,
            'x_driver_client_ids': [(6, 0, self.other_client.ids)],
        })
        self.assertEqual(self.driver.sudo().x_water_client_ids, self.other_client)

    def test_no_clients_on_a_tanker_without_a_driver(self):
        Vehicle, _domain = self._screen()
        with self.assertRaisesRegex(UserError, 'has no driver'):
            Vehicle.browse(self.spare.id).write({
                'x_driver_client_ids': [(6, 0, self.customer.ids)]})

    def test_a_driver_cannot_set_his_own_list(self):
        Vehicle = self.env['ksw.fleet.vehicle'].with_user(self.driver_user)
        with self.assertRaises(Exception):
            Vehicle.browse(self.vehicle.id).write({
                'x_driver_client_ids': [(6, 0, self.customer.ids)]})
        self.assertFalse(self.driver.sudo().x_water_client_ids)


@tagged('post_install', '-at_install')
class TestWaterAppHome(OfflineCaptureCommon):
    """Opening the app: a driver lands in the form, a supervisor in the list."""

    def _home(self, user):
        return self.env.ref('KSW_water_delivery.action_water_delivery_home').with_user(user).run()

    def test_root_menu_opens_the_home_action(self):
        root = self.env.ref('KSW_water_delivery.menu_water_delivery_root')
        self.assertEqual(root.action, self.env.ref('KSW_water_delivery.action_water_delivery_home'))

    def test_a_driver_opens_on_new_delivery(self):
        action = self._home(self.driver_user)
        self.assertEqual(action['res_model'], 'ksw.water.delivery.wizard')

    def test_a_supervisor_opens_on_the_notes_list(self):
        action = self._home(self.dispatcher_user)
        self.assertEqual(action['res_model'], 'stock.picking')
        self.assertEqual(action['target'], 'current')


@tagged('post_install', '-at_install')
class TestSupervisorTeamScope(OfflineCaptureCommon):
    """A supervisor sees and moves his own team only; an administrator sees all."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Driver Two moves to somebody else's team.
        cls.other_boss = cls.env['hr.employee'].create({'name': 'Other Supervisor'})
        cls.other_driver.parent_id = cls.other_boss
        cls.their_tanker = cls.env['ksw.fleet.vehicle'].create({
            'name': 'T-700', 'client_id': cls.company.partner_id.id,
            'vehicle_type': 'trailer', 'state': 'confirmed',
            'driver_id': cls.other_driver.id, 'x_capacity_m3': 32.0,
            'x_picking_type_id': cls.picking_type.id,
        })
        cls.tanker_group.vehicle_ids = [(4, cls.their_tanker.id)]
        cls.my_note = cls._issue_note_as(cls.driver_user, cls.vehicle)
        cls.their_note = cls._issue_note_as(cls.other_driver_user, cls.their_tanker)

    @classmethod
    def _issue_note_as(cls, user, vehicle):
        Wizard = cls.env['ksw.water.delivery.wizard'].with_user(user)
        wizard = Wizard.create(cls._wizard_vals(cls, user, vehicle_id=vehicle.id))
        wizard.action_confirm()
        return wizard.picking_id

    def _visible_notes(self, user):
        return self.env['stock.picking'].with_user(user).search(
            [('x_is_water_delivery', '=', True)])

    def test_a_supervisor_sees_only_his_teams_notes(self):
        notes = self._visible_notes(self.dispatcher_user)
        self.assertIn(self.my_note, notes)
        self.assertNotIn(self.their_note, notes)

    def test_an_administrator_sees_every_note(self):
        admin = self.env['res.users'].create({
            'name': 'Admin', 'login': 'water-admin@ksw.test',
            'group_ids': [(6, 0, [
                self.env.ref('KSW_water_delivery.group_water_dispatcher').id,
                self.env.ref('base.group_system').id])],
        })
        notes = self._visible_notes(admin)
        self.assertIn(self.my_note, notes)
        self.assertIn(self.their_note, notes)

    def test_a_driver_joining_the_team_shows_up_at_once(self):
        """The rule must not freeze the team: ir.rule domains are cached per
        user, so a resolved list of ids would hide a new team member."""
        self.assertNotIn(self.their_note, self._visible_notes(self.dispatcher_user))
        self.other_driver.parent_id = self.driver.parent_id
        self.env.flush_all()
        self.assertIn(self.their_note, self._visible_notes(self.dispatcher_user))

    def test_a_second_level_report_is_in_the_team(self):
        lead = self.env['hr.employee'].create({
            'name': 'Shift Lead', 'parent_id': self.driver.parent_id.id})
        self.other_driver.parent_id = lead
        self.env.flush_all()
        self.assertIn(self.their_note, self._visible_notes(self.dispatcher_user))

    def test_a_supervisor_cannot_move_another_teams_driver(self):
        action = self.env.ref('KSW_water_delivery.action_water_tankers')
        Vehicle = self.env['ksw.fleet.vehicle'].with_user(self.dispatcher_user).with_context(
            **dict(eval(action.context)))
        with self.assertRaisesRegex(UserError, 'not in your team'):
            Vehicle.browse(self.their_tanker.id).write({'driver_id': False})
        with self.assertRaisesRegex(UserError, 'not in your team'):
            Vehicle.browse(self.vehicle.id).write({'driver_id': self.other_driver.id})
        with self.assertRaisesRegex(UserError, 'not in your team'):
            Vehicle.browse(self.their_tanker.id).write(
                {'x_driver_client_ids': [(6, 0, self.customer.ids)]})

    def test_the_tankers_list_loads_for_a_supervisor(self):
        action = self.env.ref('KSW_water_delivery.action_water_tankers')
        views = self.env['ksw.fleet.vehicle'].with_user(self.dispatcher_user).get_views(
            [(action.view_id.id, 'list')])
        self.assertIn('parent_id.user_id', views['views']['list']['arch'])


@tagged('post_install', '-at_install')
class TestTankerGroups(OfflineCaptureCommon):
    """Tankers shows a supervisor his tanker groups only; the Employees
    Administrator is the one who decides those groups."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.isuzu = cls.env['ksw.fleet.vehicle'].create({
            'name': 'IS-900', 'client_id': cls.company.partner_id.id,
            'vehicle_type': 'isuzu', 'state': 'confirmed',
        })
        cls.hr_officer = cls.env['res.users'].create({
            'name': 'HR Officer', 'login': 'hr-officer@ksw.test',
            'group_ids': [(6, 0, [cls.env.ref('hr.group_hr_user').id])],
        })
        cls.hr_admin = cls.env['res.users'].create({
            'name': 'HR Admin', 'login': 'hr-admin@ksw.test',
            'group_ids': [(6, 0, [cls.env.ref('hr.group_hr_manager').id])],
        })

    def _screen_ids(self, user):
        action = self.env.ref('KSW_water_delivery.action_water_tankers')
        Vehicle = self.env['ksw.fleet.vehicle'].with_user(user).with_context(
            **dict(eval(action.context)))
        return Vehicle, Vehicle.search(eval(action.domain)).ids

    def test_a_supervisor_sees_only_his_groups(self):
        _Vehicle, ids = self._screen_ids(self.dispatcher_user)
        self.assertIn(self.vehicle.id, ids)
        self.assertNotIn(self.isuzu.id, ids)

    def test_a_supervisor_without_groups_sees_no_tanker(self):
        self.tanker_group.supervisor_ids = [(5, 0, 0)]
        _Vehicle, ids = self._screen_ids(self.dispatcher_user)
        self.assertFalse(ids)

    def test_an_administrator_sees_every_tanker(self):
        admin = self.env['res.users'].create({
            'name': 'Admin', 'login': 'tank-admin@ksw.test',
            'group_ids': [(6, 0, [
                self.env.ref('KSW_water_delivery.group_water_dispatcher').id,
                self.env.ref('base.group_system').id])],
        })
        _Vehicle, ids = self._screen_ids(admin)
        self.assertIn(self.isuzu.id, ids)

    def test_a_tanker_outside_his_groups_cannot_be_changed(self):
        Vehicle, _ids = self._screen_ids(self.dispatcher_user)
        with self.assertRaisesRegex(UserError, 'not in your tanker groups'):
            Vehicle.browse(self.isuzu.id).write({'x_capacity_m3': 10.0})
        with self.assertRaisesRegex(UserError, 'not in your tanker groups'):
            Vehicle.browse(self.isuzu.id).write({'driver_id': self.driver.id})

    def test_adding_a_tanker_to_his_group_shows_it_at_once(self):
        self.tanker_group.with_user(self.hr_admin).vehicle_ids = [(4, self.isuzu.id)]
        _Vehicle, ids = self._screen_ids(self.dispatcher_user)
        self.assertIn(self.isuzu.id, ids)

    def test_the_employees_administrator_attaches_groups_on_the_employee(self):
        supervisor = self.dispatcher_user.employee_id
        group = self.env['ksw.water.tanker.group'].with_user(self.hr_admin).create(
            {'name': 'Isuzu', 'vehicle_ids': [(6, 0, self.isuzu.ids)]})
        supervisor.with_user(self.hr_admin).write(
            {'x_water_tanker_group_ids': [(4, group.id)]})
        _Vehicle, ids = self._screen_ids(self.dispatcher_user)
        self.assertIn(self.isuzu.id, ids)

    def test_an_hr_officer_cannot_change_tanker_groups(self):
        supervisor = self.dispatcher_user.employee_id
        with self.assertRaises(AccessError):
            supervisor.with_user(self.hr_officer).write(
                {'x_water_tanker_group_ids': [(5, 0, 0)]})
        with self.assertRaises(AccessError):
            self.env['ksw.water.tanker.group'].with_user(self.hr_officer).create({'name': 'X'})

    def test_a_supervisor_cannot_change_tanker_groups(self):
        with self.assertRaises(AccessError):
            self.tanker_group.with_user(self.dispatcher_user).write(
                {'vehicle_ids': [(4, self.isuzu.id)]})

    def test_the_hr_admin_employee_form_loads(self):
        views = self.env['hr.employee'].with_user(self.hr_admin).get_views([(False, 'form')])
        self.assertIn('x_water_tanker_group_ids', views['views']['form']['arch'])

