from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestRequesterAssistant(TransactionCase):
    """A Requester Assistant raises requests for his requester, on the
    requester's trucks, and does nothing else."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)
        Employee = cls.env['hr.employee']
        requester = ('base.group_user', 'KSW_workshop.group_workshop_requester')
        assistant = ('base.group_user', 'KSW_workshop.group_workshop_requester_assistant')

        def _user(login, groups):
            return Users.create({
                'name': login, 'login': login, 'email': f'{login}@wra.test',
                'group_ids': [(6, 0, [cls.env.ref(g).id for g in groups])]})

        cls.sup_user = _user('wra_supervisor', requester)
        cls.supervisor = Employee.create({'name': 'WRA Supervisor', 'user_id': cls.sup_user.id})
        cls.other_user = _user('wra_other', requester)
        cls.other_sup = Employee.create({'name': 'WRA Other', 'user_id': cls.other_user.id})
        cls.boss = Employee.create({'name': 'WRA Boss Without Login'})

        cls.asst_user = _user('wra_assistant', assistant)
        cls.asst = Employee.create({'name': 'WRA Assistant', 'user_id': cls.asst_user.id,
                                    'parent_id': cls.supervisor.id})
        cls.stray_user = _user('wra_stray', assistant)
        Employee.create({'name': 'WRA Stray', 'user_id': cls.stray_user.id,
                         'parent_id': cls.boss.id})

        cls.driver = Employee.create({'name': 'WRA Driver', 'parent_id': cls.supervisor.id})
        cls.outsider = Employee.create({'name': 'WRA Outsider'})
        Vehicle = cls.env['ksw.fleet.vehicle']
        cls.group_truck = Vehicle.create({'name': 'IS9201', 'vehicle_type': 'isuzu'})
        cls.team_truck = Vehicle.create({'name': 'IS9202', 'vehicle_type': 'isuzu',
                                         'driver_id': cls.driver.id})
        cls.foreign_truck = Vehicle.create({'name': 'IS9203', 'vehicle_type': 'isuzu',
                                            'driver_id': cls.outsider.id})
        cls.other_truck = Vehicle.create({'name': 'IS9204', 'vehicle_type': 'isuzu'})
        Group = cls.env['ksw.water.tanker.group']
        Group.create({'name': 'WRA Group', 'vehicle_ids': [(6, 0, cls.group_truck.ids)],
                      'supervisor_ids': [(6, 0, cls.supervisor.ids)]})
        Group.create({'name': 'WRA Other Group', 'vehicle_ids': [(6, 0, cls.other_truck.ids)],
                      'supervisor_ids': [(6, 0, cls.other_sup.ids)]})

    def _pickable(self, user):
        return self.env['ksw.fleet.vehicle'].with_user(user).search(
            [('x_workshop_in_scope', '=', True), ('name', 'like', 'IS92')])

    def _raise(self, user, vehicle, description='Oil change'):
        return self.env['ksw.workshop.request'].with_user(user).create({
            'vehicle_type': 'isuzu', 'vehicle_id': vehicle.id,
            'driver_id': self.driver.id, 'description': description})

    # ----------------------------------------------------------------- scope
    def test_assistant_picks_exactly_his_managers_trucks(self):
        self.assertEqual(self._pickable(self.asst_user), self._pickable(self.sup_user))
        self.assertEqual(self._pickable(self.asst_user), self.group_truck | self.team_truck)

    def test_assistant_outside_the_managers_trucks_is_refused(self):
        with self.assertRaises(UserError):
            self._raise(self.asst_user, self.foreign_truck)

    def test_assistant_of_nobody_raises_nothing(self):
        self.assertFalse(self._pickable(self.stray_user))
        with self.assertRaises(UserError):
            self._raise(self.stray_user, self.group_truck)

    def test_manager_without_requester_role_is_not_assisted(self):
        self.sup_user.write({'group_ids': [(3, self.env.ref('KSW_workshop.group_workshop_requester').id)]})
        self.assertEqual(self.asst_user._ksw_workshop_assisted_manager_ids(), [])

    # -------------------------------------------------------------- request
    def test_request_is_the_assistants_on_behalf_of_his_manager(self):
        request = self._raise(self.asst_user, self.group_truck)
        self.assertEqual(request.employee_id, self.asst)
        self.assertEqual(request.x_on_behalf_of_id, self.sup_user)

    def test_manager_reads_it_but_cannot_change_it(self):
        request = self._raise(self.asst_user, self.team_truck)
        mine = self.env['ksw.workshop.request'].with_user(self.sup_user).search(
            [('id', '=', request.id)])
        self.assertEqual(mine, request)
        # The requester-fields guard answers first (Pitfall #12); the record
        # rules would refuse it too, the on-behalf rule being read-only.
        with self.assertRaises(UserError):
            request.with_user(self.sup_user).write({'description': 'Brakes'})

    def test_assistant_does_not_see_the_managers_own_requests(self):
        own = self._raise(self.sup_user, self.group_truck)
        seen = self.env['ksw.workshop.request'].with_user(self.asst_user).search([('id', '=', own.id)])
        self.assertFalse(seen)

    def test_on_behalf_cannot_be_set_by_the_client(self):
        request = self._raise(self.asst_user, self.group_truck)
        request.with_user(self.asst_user).write({'x_on_behalf_of_id': self.other_user.id})
        self.assertEqual(request.x_on_behalf_of_id, self.sup_user)

    def test_named_requester_follows_the_truck(self):
        self.asst_user.x_workshop_assisted_manager_ids = self.other_user
        self.assertIn(self.other_truck, self._pickable(self.asst_user))
        request = self._raise(self.asst_user, self.other_truck)
        self.assertEqual(request.x_on_behalf_of_id, self.other_user)
        request.with_user(self.asst_user).write({'vehicle_id': self.group_truck.id})
        self.assertEqual(request.x_on_behalf_of_id, self.sup_user)

    # ------------------------------------------------------------- the role
    def test_role_grants_nothing_but_internal_user(self):
        group = self.env.ref('KSW_workshop.group_workshop_requester_assistant')
        self.assertEqual(group.all_implied_ids - group, self.env.ref('base.group_user').all_implied_ids
                         | self.env.ref('base.group_user'))

    def test_hr_manager_assistant_is_not_a_workshop_assistant(self):
        hr_assistant = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'wra_hr', 'login': 'wra_hr',
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id,
                                  self.env.ref('KSW_base_security.group_manager_assistant').id])]})
        self.env['hr.employee'].create({'name': 'WRA HR', 'user_id': hr_assistant.id,
                                        'parent_id': self.supervisor.id})
        with self.assertRaises(AccessError):
            self._raise(hr_assistant, self.group_truck)
