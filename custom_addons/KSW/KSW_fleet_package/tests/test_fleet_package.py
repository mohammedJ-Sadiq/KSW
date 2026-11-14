from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestFleetPackage(TransactionCase):
    """Vehicles released together with their drivers: no workshop, no HR-12,
    custody unchanged, and the drivers transferred the same day."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Department = cls.env['hr.department']
        cls.dept_a = Department.create({'name': 'PK Alpha', 'x_fleet_holder': True})
        cls.dept_b = Department.create({'name': 'PK Bravo', 'x_fleet_holder': True})
        Users = cls.env['res.users'].with_context(no_reset_password=True)

        def _user(login, group, departments=()):
            user = Users.create({'name': login, 'login': login, 'email': f'{login}@pk.test',
                                 'group_ids': [(6, 0, [cls.env.ref(group).id])],
                                 'x_fleet_department_ids': [(6, 0, [d.id for d in departments])]})
            cls.env['hr.employee'].create({'name': login, 'user_id': user.id})
            return user

        cls.user_a = _user('pk_user_a', 'KSW_fleet_allocation.group_fleet_allocation_user', [cls.dept_a])
        cls.user_b = _user('pk_user_b', 'KSW_fleet_allocation.group_fleet_allocation_user', [cls.dept_b])
        cls.gm = _user('pk_gm', 'KSW_fleet_allocation.group_fleet_allocation_gm')
        cls.hr = _user('pk_hr', 'KSW_vehicle_custody.group_custody_hr')
        cls.accounting = _user('pk_acc', 'KSW_vehicle_custody.group_custody_accounting')
        Employee = cls.env['hr.employee']
        cls.driver_1 = Employee.create({'name': 'PK Driver 1', 'department_id': cls.dept_a.id})
        cls.driver_2 = Employee.create({'name': 'PK Driver 2', 'department_id': cls.dept_a.id})
        Vehicle = cls.env['ksw.fleet.vehicle']
        cls.truck_1 = Vehicle.create({'name': 'PK-1', 'vehicle_type': 'isuzu', 'x_department_id': cls.dept_a.id})
        cls.truck_2 = Vehicle.create({'name': 'PK-2', 'vehicle_type': 'isuzu', 'x_department_id': cls.dept_a.id})
        cls.truck_1._ksw_set_custodian(cls.driver_1)
        cls.truck_2._ksw_set_custodian(cls.driver_2)

    def _release(self, lines):
        request = self.env['ksw.fleet.request'].with_user(self.user_a).create({
            'request_type': 'release', 'department_id': self.dept_a.id, 'reason': 'Contract moves to Bravo',
            'release_line_ids': [(0, 0, vals) for vals in lines]})
        request.action_submit()
        return request

    def _need(self, quantity, need):
        request = self.env['ksw.fleet.request'].with_user(self.user_b).create({
            'request_type': 'addition', 'department_id': self.dept_b.id, 'reason': 'New contract',
            'addition_line_ids': [(0, 0, {'vehicle_type': 'isuzu', 'quantity': quantity, 'need': need})]})
        request.action_submit()
        return request.addition_line_ids

    def _decide(self, lines, **vals):
        self.env['ksw.fleet.release.decision.wizard'].with_user(self.gm).create(
            dict({'release_line_ids': [(6, 0, lines.ids)]}, **vals)).action_confirm()

    def test_vehicles_move_with_their_drivers(self):
        request = self._release([{'vehicle_id': self.truck_1.id, 'x_with_driver': True},
                                 {'vehicle_id': self.truck_2.id, 'x_with_driver': True}])
        self.assertEqual(request.release_line_ids.x_driver_id, self.driver_1 | self.driver_2)
        need = self._need(2, 'with_driver')
        self._decide(request.release_line_ids, decision='transfer', addition_line_id=need.id)
        self.assertFalse(request.sudo().x_custody_request_ids)  # no HR-12 for a package
        for truck, driver in ((self.truck_1, self.driver_1), (self.truck_2, self.driver_2)):
            self.assertEqual((truck.x_department_id, truck.x_allocation_state), (self.dept_b, 'assigned'))
            self.assertEqual(truck.x_custodian_id, driver)  # custody unchanged
            self.assertFalse(truck.x_checkup_request_id)
            self.assertEqual(driver.department_id, self.dept_b)
        transfers = request.release_line_ids.x_employee_transfer_id
        self.assertEqual(transfers.mapped('state'), ['applied', 'applied'])
        self.assertTrue(all(transfers.mapped('with_vehicles')))
        self.assertEqual(request.state, 'done')
        self.assertEqual(need.request_id.state, 'done')
        copy = request.message_ids.filtered(lambda m: self.accounting.partner_id in m.partner_ids)
        self.assertTrue(copy)

    def test_needs_decide_what_fills_them(self):
        request = self._release([{'vehicle_id': self.truck_1.id, 'x_with_driver': True},
                                 {'vehicle_id': self.truck_2.id}])
        package, alone = request.release_line_ids.sorted(lambda l: not l.x_with_driver)
        vehicle_only = self._need(1, 'vehicle')
        with self.assertRaises(UserError):  # comes with a driver, the need has its own
            self._decide(package, decision='transfer', addition_line_id=vehicle_only.id)
        with self.assertRaises(UserError):  # with and without drivers in one decision
            self._decide(request.release_line_ids, decision='transfer', addition_line_id=vehicle_only.id)
        self._decide(alone, decision='transfer', addition_line_id=vehicle_only.id)
        self.assertEqual((self.truck_2.x_allocation_state, self.truck_2.x_reserved_department_id),
                         ('holding', self.dept_b))  # through the workshop
        self.assertEqual(self.driver_2.department_id, self.dept_a)  # his driver stays
        either = self._need(1, 'either')
        self._decide(package, decision='transfer', addition_line_id=either.id)
        self.assertEqual(self.truck_1.x_department_id, self.dept_b)  # at once

    def test_package_cannot_go_to_the_workshop(self):
        request = self._release([{'vehicle_id': self.truck_1.id, 'x_with_driver': True}])
        with self.assertRaises(UserError):
            self._decide(request.release_line_ids, decision='holding')
        self._decide(request.release_line_ids, decision='keep', note='Contract extended')
        self.assertEqual(self.truck_1.x_department_id, self.dept_a)
        self.assertEqual(self.driver_1.department_id, self.dept_a)

    def test_package_needs_a_custodian(self):
        truck = self.env['ksw.fleet.vehicle'].create({'name': 'PK-3', 'vehicle_type': 'isuzu',
                                                      'x_department_id': self.dept_a.id})
        request = self.env['ksw.fleet.request'].with_user(self.user_a).create({
            'request_type': 'release', 'department_id': self.dept_a.id, 'reason': 'x',
            'release_line_ids': [(0, 0, {'vehicle_id': truck.id, 'x_with_driver': True})]})
        with self.assertRaises(UserError):
            request.action_submit()

    def test_roles_line_up(self):
        self.assertTrue(self.gm.has_group('KSW_employee_transfer.group_transfer_gm'))
        self.assertTrue(self.hr.has_group('KSW_employee_transfer.group_transfer_hr'))
        self.assertTrue(self.user_a.has_group('KSW_employee_transfer.group_transfer_requester'))
        transfer = self.env['ksw.employee.transfer'].with_user(self.user_a).create({
            'employee_id': self.env['hr.employee'].create({'name': 'PK Clerk', 'department_id': self.dept_a.id}).id,
            'to_department_id': self.dept_b.id, 'reason': 'Needed there'})
        self.assertEqual(transfer.from_department_id, self.dept_a)
