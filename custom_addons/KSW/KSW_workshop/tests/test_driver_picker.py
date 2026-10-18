"""The request's Driver picker offers the requester's reporting line only,
and saving refuses anyone else. Nothing set up is an empty list, never the
whole company."""
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestDriverPicker(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)
        Employee = cls.env['hr.employee']

        def _user(login, groups):
            return Users.create({
                'name': login, 'login': login, 'email': f'{login}@drv.test',
                'group_ids': [(6, 0, [cls.env.ref(g).id for g in groups])]})

        requester = ('base.group_user', 'KSW_workshop.group_workshop_requester')
        cls.sup_user = _user('drv_sup', requester)
        cls.supervisor = Employee.create({'name': 'DRV Supervisor', 'user_id': cls.sup_user.id})
        cls.driver = Employee.create({'name': 'DRV Driver', 'parent_id': cls.supervisor.id})
        cls.sub_driver = Employee.create({'name': 'DRV Sub Driver', 'parent_id': cls.driver.id})
        cls.outsider = Employee.create({'name': 'DRV Outsider'})
        cls.mgr_user = _user('drv_mgr', ('base.group_user', 'KSW_workshop.group_workshop_manager'))
        Employee.create({'name': 'DRV Manager', 'user_id': cls.mgr_user.id})
        cls.no_emp_user = _user('drv_noemp', requester)
        cls.asst_user = _user('drv_asst', ('base.group_user', 'KSW_workshop.group_workshop_requester_assistant'))
        Employee.create({'name': 'DRV Assistant', 'user_id': cls.asst_user.id,
                         'parent_id': cls.supervisor.id})
        cls.vehicle = cls.env['ksw.fleet.vehicle'].create({
            'name': 'DRV1', 'vehicle_type': 'isuzu', 'driver_id': cls.supervisor.id})

    def _pickable(self, user):
        Employee = self.env['hr.employee'].with_user(user).with_context(ksw_workshop_driver_picker=True)
        return {r[0] for r in Employee.name_search('DRV', limit=None)}

    def _raise(self, user, driver):
        return self.env['ksw.workshop.request'].with_user(user).create({
            'vehicle_type': 'isuzu', 'vehicle_id': self.vehicle.id,
            'driver_id': driver.id, 'description': 'Oil change'})

    def test_requester_picks_from_his_reporting_line_only(self):
        picked = self._pickable(self.sup_user)
        self.assertTrue({self.supervisor.id, self.driver.id, self.sub_driver.id} <= picked)
        self.assertNotIn(self.outsider.id, picked)

    def test_the_picker_alone_is_not_narrowed_elsewhere(self):
        # Without the picker context, nothing changes for the same user.
        names = self.env['hr.employee'].with_user(self.sup_user).name_search('DRV Outsider')
        self.assertIn(self.outsider.id, [r[0] for r in names])

    def test_saving_an_outside_driver_is_refused(self):
        self.assertEqual(self._raise(self.sup_user, self.driver).driver_id, self.driver)
        with self.assertRaises(UserError):
            self._raise(self.sup_user, self.outsider)

    def test_changing_to_an_outside_driver_is_refused(self):
        request = self._raise(self.sup_user, self.driver)
        with self.assertRaises(UserError):
            request.with_user(self.sup_user).write({'driver_id': self.outsider.id})

    def test_workshop_manager_picks_anyone(self):
        self.assertIn(self.outsider.id, self._pickable(self.mgr_user))
        self.env['ksw.workshop.request'].with_user(self.mgr_user).create({
            'vehicle_type': 'isuzu', 'vehicle_id': self.vehicle.id,
            'driver_id': self.outsider.id, 'description': 'Oil change'})

    def test_user_without_employee_picks_nobody(self):
        self.assertFalse(self._pickable(self.no_emp_user))

    def test_assistant_picks_from_his_requesters_line(self):
        picked = self._pickable(self.asst_user)
        self.assertIn(self.driver.id, picked)
        self.assertNotIn(self.outsider.id, picked)


@tagged('post_install', '-at_install')
class TestDriverPickerHrRoute(TransactionCase):
    """A user holding an Employees group reads hr.employee itself, where
    KSW_base_security limits him to himself and his immediate subordinates;
    the picker must still offer his whole allowed line (a.mutawa, KSWCO
    2026-10-18: an assistant saw only his own name). The class above never
    reaches this path: its users are answered by hr.employee.public."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)
        Employee = cls.env['hr.employee']
        hr_supervisor = 'KSW_base_security.group_hr_employee_supervisor'

        def _user(login, groups):
            return Users.create({
                'name': login, 'login': login, 'email': f'{login}@drvhr.test',
                'group_ids': [(6, 0, [cls.env.ref(g).id for g in groups])]})

        cls.sup_user = _user('drvhr_sup', ('KSW_workshop.group_workshop_requester', hr_supervisor))
        cls.supervisor = Employee.create({'name': 'DRVHR Supervisor', 'user_id': cls.sup_user.id})
        cls.driver = Employee.create({'name': 'DRVHR Driver', 'parent_id': cls.supervisor.id})
        cls.sub_driver = Employee.create({'name': 'DRVHR Sub Driver', 'parent_id': cls.driver.id})
        cls.outsider = Employee.create({'name': 'DRVHR Outsider'})
        cls.asst_user = _user('drvhr_asst', (
            'KSW_workshop.group_workshop_requester_assistant', hr_supervisor))
        cls.assistant = Employee.create({'name': 'DRVHR Assistant', 'user_id': cls.asst_user.id,
                                         'parent_id': cls.supervisor.id})
        cls.other_sup_user = _user('drvhr_sup2', ('KSW_workshop.group_workshop_requester',))
        cls.other_supervisor = Employee.create({'name': 'DRVHR Other Supervisor',
                                                'user_id': cls.other_sup_user.id})
        cls.other_driver = Employee.create({'name': 'DRVHR Other Driver',
                                            'parent_id': cls.other_supervisor.id})

    def _pickable(self, user):
        self.env.invalidate_all()
        Employee = self.env['hr.employee'].with_user(user).with_context(ksw_workshop_driver_picker=True)
        self.assertTrue(Employee.browse().has_access('read'), 'fixture must take the hr.employee route')
        return {r[0] for r in Employee.name_search('DRVHR', limit=None)}

    def test_assistant_picks_his_requesters_whole_line(self):
        picked = self._pickable(self.asst_user)
        self.assertTrue({self.supervisor.id, self.driver.id, self.sub_driver.id,
                         self.assistant.id} <= picked)
        self.assertNotIn(self.outsider.id, picked)
        self.assertNotIn(self.other_driver.id, picked)

    def test_requester_picks_beyond_immediate_subordinates(self):
        picked = self._pickable(self.sup_user)
        self.assertIn(self.sub_driver.id, picked)
        self.assertNotIn(self.outsider.id, picked)

    def test_line_is_read_only(self):
        with self.assertRaises(AccessError):
            self.driver.with_user(self.asst_user).write({'name': 'Renamed'})

    def test_moving_the_assistant_moves_his_line(self):
        self.assertIn(self.driver.id, self._pickable(self.asst_user))
        self.assistant.write({'parent_id': self.other_supervisor.id})
        picked = self._pickable(self.asst_user)
        self.assertIn(self.other_driver.id, picked)
        self.assertNotIn(self.driver.id, picked)
