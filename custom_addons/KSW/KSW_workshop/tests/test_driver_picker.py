"""The request's Driver picker offers the requester's reporting line only,
and saving refuses anyone else. Nothing set up is an empty list, never the
whole company."""
from odoo.exceptions import UserError
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
