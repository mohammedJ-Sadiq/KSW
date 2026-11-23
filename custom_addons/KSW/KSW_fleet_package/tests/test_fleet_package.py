from datetime import date, timedelta

from odoo.exceptions import RedirectWarning, UserError
from odoo.tests import TransactionCase, tagged

def _register_papers(env, vehicle):
    """A company vehicle's handover papers, valid (the register as HR keeps it)."""
    for doc_type, _status in vehicle._ksw_handover_gaps().get(vehicle.id, []):
        env['ksw.document'].create({
            'type_id': doc_type.id, 'applies_to': 'vehicle', 'vehicle_id': vehicle.id,
            'number': 'P-%s-%s' % (vehicle.id, doc_type.id),
            'expiry_date': date.today() + timedelta(days=365)}).action_confirm()


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
        for truck in (cls.truck_1, cls.truck_2):
            _register_papers(cls.env, truck)
        cls.driver_3 = Employee.create({'name': 'PK Driver 3', 'department_id': cls.dept_a.id})
        for driver in (cls.driver_1, cls.driver_2, cls.driver_3):
            cls._licence(driver)

    @classmethod
    def _licence(cls, employee, expiry=None):
        scan = cls.env['ir.attachment'].create({'name': 'licence.png', 'raw': b'\x89PNG fake',
                                                'mimetype': 'image/png'})
        doc = cls.env['ksw.document'].create({
            'type_id': cls.env.ref('KSW_documents.type_driving_licence').id, 'number': f'L{employee.id}',
            'expiry_date': expiry or date.today() + timedelta(days=365), 'applies_to': 'employee',
            'employee_id': employee.id, 'attachment_ids': [(6, 0, scan.ids)]})
        doc.action_confirm()
        return doc

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
        # A fleet user releases his department's people (no direct transfer).
        clerk = self.env['hr.employee'].create({'name': 'PK Clerk', 'department_id': self.dept_a.id})
        request = self.env['ksw.employee.request'].with_user(self.user_a).create({
            'request_type': 'release', 'department_id': self.dept_a.id, 'reason': 'Needed elsewhere',
            'release_line_ids': [(0, 0, {'employee_id': clerk.id})]})
        request.action_submit()
        self.assertEqual(request.state, 'submitted')

    # ------------------------------------------------------------------
    # An employee released while he answers for vehicles: his department
    # says what happens to each, and it happens only when he actually leaves.

    def _release_employee(self, disposition=None, new_driver=None, submit=True):
        request = self.env['ksw.employee.request'].with_user(self.user_a).create({
            'request_type': 'release', 'department_id': self.dept_a.id, 'reason': 'Route closed',
            'release_line_ids': [(0, 0, {'employee_id': self.driver_1.id})]})
        row = request.x_vehicle_ids
        if disposition:
            row.with_user(self.user_a).write({'disposition': disposition,
                                              'new_driver_id': new_driver and new_driver.id})
        if submit:
            request.action_submit()
        return request

    def _ask_employee(self):
        request = self.env['ksw.employee.request'].with_user(self.user_b).create({
            'request_type': 'addition', 'department_id': self.dept_b.id, 'reason': 'New route',
            'addition_line_ids': [(0, 0, {'job_id': self.env['hr.job'].create({'name': 'PK Job'}).id})]})
        request.action_submit()
        return request.addition_line_ids

    def _gm(self, request, decision, **vals):
        self.env['ksw.employee.release.decision.wizard'].with_user(self.gm).create(dict({
            'release_line_ids': [(6, 0, request.release_line_ids.ids)], 'decision': decision,
            'note': 'GM'}, **vals)).action_confirm()

    def test_each_vehicle_needs_a_plan(self):
        request = self._release_employee(submit=False)
        self.assertEqual(request.x_vehicle_ids.vehicle_id, self.truck_1)
        with self.assertRaises(UserError):
            request.action_submit()
        request.x_vehicle_ids.with_user(self.user_a).disposition = 'driver_change'
        with self.assertRaises(UserError):
            request.action_submit()  # no new driver
        outsider = self.env['hr.employee'].create({'name': 'PK Bravo Man', 'department_id': self.dept_b.id})
        request.x_vehicle_ids.with_user(self.user_a).new_driver_id = outsider
        with self.assertRaises(UserError):
            request.action_submit()  # driver change stays in the vehicle's department
        # He already drives PK-2: not offered, and refused if forced.
        self.assertNotIn(self.driver_2.id, request.x_vehicle_ids.allowed_new_driver_ids.ids)
        self.assertIn(self.driver_3.id, request.x_vehicle_ids.allowed_new_driver_ids.ids)
        request.x_vehicle_ids.with_user(self.user_a).new_driver_id = self.driver_2
        with self.assertRaisesRegex(UserError, 'already answers for PK-2'):
            request.action_submit()
        request.x_vehicle_ids.with_user(self.user_a).new_driver_id = self.driver_3
        request.action_submit()
        self.assertEqual(request.state, 'submitted')

    def test_driver_change_when_he_leaves_and_move_waits_for_it(self):
        request = self._release_employee('driver_change', self.driver_3)
        self.assertFalse(request.x_vehicle_ids.custody_request_id)  # nothing before the GM
        self._gm(request, 'transfer', addition_line_id=self._ask_employee().id)
        # A driver change is the department's own: done with the GM's decision.
        form = request.x_vehicle_ids.custody_request_id
        self.assertEqual((form.kind, form.state, form.new_employee_id), ('driver_change', 'done', self.driver_3))
        self.assertFalse(form.workshop_request_id)
        self.assertEqual(self.truck_1.x_custodian_id, self.driver_3)
        transfer = request.release_line_ids.sudo().transfer_id
        transfer.with_user(self.hr).write({'to_manager_id': self.user_b.employee_id.id,
                                           'to_leave_manager_id': self.user_b.id,
                                           'to_hr_responsible_id': self.hr.id})
        transfer.with_user(self.hr).action_hr_approve()
        self.assertEqual(transfer.state, 'applied')  # he answers for no vehicle any more
        self.assertEqual(self.driver_1.department_id, self.dept_b)

    def test_release_to_workshop_on_end_of_service(self):
        request = self._release_employee('holding')
        self._gm(request, 'terminate')
        fleet_request = request.x_vehicle_ids.fleet_request_id
        self.assertEqual((fleet_request.request_type, fleet_request.state), ('release', 'submitted'))
        self.assertEqual(fleet_request.release_line_ids.vehicle_id, self.truck_1)
        self.assertEqual(fleet_request.department_id, self.dept_a)

    def test_kept_employee_keeps_his_vehicle(self):
        request = self._release_employee('holding')
        self._gm(request, 'keep')
        self.assertFalse(request.x_vehicle_ids.fleet_request_id)
        self.assertEqual(self.truck_1.x_custodian_id, self.driver_1)

    # ------------------------------------------------------------------
    # The plan for a leaving driver's vehicles is checked where it is made,
    # and the GM sees it before deciding (ER/2026/0264: two trucks to one man
    # with no licence on file, decided from a list of names).

    def _release_both(self, plan):
        request = self.env['ksw.employee.request'].with_user(self.user_a).create({
            'request_type': 'release', 'department_id': self.dept_a.id, 'reason': 'Route closed',
            'release_line_ids': [(0, 0, {'employee_id': self.driver_1.id}),
                                 (0, 0, {'employee_id': self.driver_2.id})]})
        for row in request.x_vehicle_ids:
            disposition, driver = plan[row.vehicle_id]
            row.with_user(self.user_a).write({'disposition': disposition, 'new_driver_id': driver.id})
        return request

    def test_one_new_driver_per_vehicle(self):
        request = self._release_both({self.truck_1: ('driver_change', self.driver_3),
                                      self.truck_2: ('driver_change', self.driver_3)})
        with self.assertRaisesRegex(UserError, 'already the new driver'):
            request.action_submit()
        request.x_vehicle_ids.filtered(lambda r: r.vehicle_id == self.truck_2).with_user(self.user_a).write(
            {'disposition': 'holding', 'new_driver_id': False})
        request.action_submit()
        # Another release cannot hand him a second one while this one waits.
        self.truck_2.sudo().x_custodian_id = False
        other_truck = self.env['ksw.fleet.vehicle'].create({'name': 'PK-3', 'vehicle_type': 'isuzu',
                                                           'x_department_id': self.dept_a.id})
        leaver = self.env['hr.employee'].create({'name': 'PK Leaver', 'department_id': self.dept_a.id})
        other_truck._ksw_set_custodian(leaver)
        second = self.env['ksw.employee.request'].with_user(self.user_a).create({
            'request_type': 'release', 'department_id': self.dept_a.id, 'reason': 'Route closed',
            'release_line_ids': [(0, 0, {'employee_id': leaver.id})]})
        second.x_vehicle_ids.with_user(self.user_a).write({'disposition': 'driver_change',
                                                           'new_driver_id': self.driver_3.id})
        self.assertNotIn(self.driver_3.id, second.x_vehicle_ids.allowed_new_driver_ids.ids)
        with self.assertRaisesRegex(UserError, 'already the new driver'):
            second.action_submit()

    def test_new_driver_is_not_leaving_himself(self):
        request = self._release_both({self.truck_1: ('driver_change', self.driver_2),
                                      self.truck_2: ('holding', self.env['hr.employee'])})
        with self.assertRaisesRegex(UserError, 'being released'):
            request.action_submit()

    def test_new_driver_needs_a_valid_licence(self):
        newcomer = self.env['hr.employee'].create({'name': 'PK No Licence', 'department_id': self.dept_a.id})
        request = self._release_employee('driver_change', newcomer, submit=False)
        with self.assertRaisesRegex(RedirectWarning, r'Driving Licence \(missing\)'):
            request.action_submit()
        self._licence(newcomer, expiry=date.today() - timedelta(days=1))
        with self.assertRaisesRegex(RedirectWarning, r'Driving Licence \(expired\)'):
            request.action_submit()
        self._licence(newcomer)
        request.action_submit()
        self.assertEqual(request.state, 'submitted')

    def test_supervisor_asks_hr_for_the_licence(self):
        newcomer = self.env['hr.employee'].create({'name': 'PK No Licence', 'department_id': self.dept_a.id})
        request = self._release_employee('driver_change', newcomer, submit=False)
        with self.assertRaises(RedirectWarning) as caught:
            request.with_user(self.user_a).action_submit()
        action, context = caught.exception.args[1], caught.exception.args[3]
        self.assertEqual(self.env.ref(action), self.env.ref('KSW_fleet_package.action_request_driver_licence'))
        row = self.env[context['active_model']].browse(context['active_id'])
        self.assertEqual(row, request.x_vehicle_ids)
        row.with_user(self.user_a).action_request_documents()
        dr = self.env['ksw.document.request'].search([('employee_id', '=', newcomer.id)])
        self.assertEqual((dr.state, dr.requested_by_id, dr.origin), ('requested', self.user_a, request.name))
        self.assertIn(dr.name, row.document_requests)
        # The supervisor follows it himself; asking again files nothing new.
        self.assertEqual(self.env['ksw.document.request'].with_user(self.user_a).search([]), dr)
        row.with_user(self.user_a).action_request_documents()
        self.assertEqual(self.env['ksw.document.request'].search_count([('employee_id', '=', newcomer.id)]), 1)
        with self.assertRaisesRegex(UserError, 'HR has DR/'):
            request.with_user(self.user_a).action_submit()
        # HR registers it from the request: closed, he is told, the release goes.
        doc = self.env['ksw.document'].browse(dr.action_register()['res_id'])
        doc.write({'number': 'L-NEW', 'expiry_date': date.today() + timedelta(days=300)})
        doc.action_confirm()
        self.assertEqual(dr.state, 'done')
        self.assertTrue(dr.message_ids.filtered(lambda m: self.user_a.partner_id in m.partner_ids))
        request.with_user(self.user_a).action_submit()
        self.assertEqual(request.state, 'submitted')
        # Only a draft release asks; the GM's recheck is a plain refusal.
        with self.assertRaises(UserError):
            row.with_user(self.user_a).action_request_documents()

    def test_gm_sees_the_plan_and_it_is_rechecked(self):
        request = self._release_employee('driver_change', self.driver_3)
        row = request.x_vehicle_ids
        note = request.message_ids.filtered(lambda m: 'PK-1' in (m.body or ''))
        self.assertIn('PK Driver 3', note.body)
        wizard = self.env['ksw.employee.release.decision.wizard'].with_user(self.gm).create({
            'release_line_ids': [(6, 0, request.release_line_ids.ids)], 'decision': 'terminate'})
        self.assertEqual(wizard.x_vehicle_ids, row)
        # His licence lapses while the request waits: the GM's decision stops.
        self.env['ksw.vehicle.custody.request']._ksw_register_licence(self.driver_3).expiry_date = \
            date.today() - timedelta(days=1)
        with self.assertRaisesRegex(UserError, r'Driving Licence \(expired\)'):
            wizard.action_confirm()
        self.assertFalse(row.custody_request_id)
