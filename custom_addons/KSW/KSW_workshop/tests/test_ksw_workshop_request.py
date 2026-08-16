from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestKswWorkshopRequest(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _mkuser(name, login, group_xmlids=('base.group_user',)):
            return cls.env['res.users'].create({
                'name': name,
                'login': login,
                'email': f'{login}@wsreq.test',
                'group_ids': [(6, 0, [cls.env.ref(xmlid).id for xmlid in group_xmlids])],
            })

        cls.user_employee = _mkuser('WS Employee', 'wsreq_employee')
        cls.employee = cls.env['hr.employee'].create({
            'name': 'WS Employee', 'user_id': cls.user_employee.id,
        })

        cls.user_other = _mkuser('WS Other Employee', 'wsreq_other')
        cls.other_employee = cls.env['hr.employee'].create({
            'name': 'WS Other Employee', 'user_id': cls.user_other.id,
        })

        cls.user_manager = _mkuser(
            'WS Manager', 'wsreq_manager',
            group_xmlids=('base.group_user', 'KSW_workshop.group_workshop_manager'),
        )
        cls.manager_employee = cls.env['hr.employee'].create({
            'name': 'WS Manager', 'user_id': cls.user_manager.id,
        })

        cls.user_technician = _mkuser(
            'WS Technician', 'wsreq_technician',
            group_xmlids=('base.group_user', 'KSW_workshop.group_workshop_technician'),
        )
        cls.technician_employee = cls.env['hr.employee'].create({
            'name': 'WS Technician', 'user_id': cls.user_technician.id,
        })

        cls.vehicle = cls.env['ksw.fleet.vehicle'].create({'name': 'WS-152', 'vehicle_model': 'سياب'})

    def _make_request(self, employee_user=None, **kwargs):
        vals = {
            'vehicle_id': self.vehicle.id,
            'description': 'Battery problem',
        }
        vals.update(kwargs)
        model = self.env['ksw.workshop.request']
        if employee_user:
            model = model.with_user(employee_user)
        return model.create(vals)

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    def test_sequence_name_assigned(self):
        request = self._make_request(employee_user=self.user_employee)
        self.assertTrue(request.name.startswith('WS'))
        self.assertNotEqual(request.name, 'New')

    def test_employee_cannot_impersonate_another_requester(self):
        request = self._make_request(
            employee_user=self.user_employee, employee_id=self.other_employee.id,
        )
        self.assertEqual(request.employee_id, self.employee)

    def test_default_state_is_new(self):
        request = self._make_request(employee_user=self.user_employee)
        self.assertEqual(request.state, 'new')

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------
    def test_employee_cannot_start_request(self):
        request = self._make_request(employee_user=self.user_employee)
        with self.assertRaises(UserError):
            request.with_user(self.user_employee).action_start()

    def test_manager_can_start_request(self):
        request = self._make_request(employee_user=self.user_employee)
        request.with_user(self.user_manager).action_start()
        self.assertEqual(request.state, 'in_progress')

    def test_cannot_start_twice(self):
        request = self._make_request(employee_user=self.user_employee)
        request.with_user(self.user_manager).action_start()
        with self.assertRaises(UserError):
            request.with_user(self.user_manager).action_start()

    def test_reject_requires_reason(self):
        request = self._make_request(employee_user=self.user_employee)
        with self.assertRaises(UserError):
            request.with_user(self.user_manager).action_reject()

    def test_manager_can_reject_with_reason(self):
        request = self._make_request(employee_user=self.user_employee)
        request.with_user(self.user_manager).write({'rejection_reason': 'Duplicate request'})
        request.with_user(self.user_manager).action_reject()
        self.assertEqual(request.state, 'rejected')

    def test_employee_cannot_set_state_directly(self):
        request = self._make_request(employee_user=self.user_employee)
        with self.assertRaises(UserError):
            request.with_user(self.user_employee).write({'state': 'in_progress'})

    def test_manager_can_complete_in_progress_request(self):
        request = self._make_request(employee_user=self.user_employee)
        request.with_user(self.user_manager).action_start()
        request.with_user(self.user_manager).action_complete()
        self.assertEqual(request.state, 'completed')
        self.assertTrue(request.completion_date)

    def test_cannot_complete_new_request(self):
        request = self._make_request(employee_user=self.user_employee)
        with self.assertRaises(UserError):
            request.with_user(self.user_manager).action_complete()

    # ------------------------------------------------------------------
    # Repair report access
    # ------------------------------------------------------------------
    def test_technician_can_edit_report_while_in_progress(self):
        request = self._make_request(employee_user=self.user_employee)
        request.with_user(self.user_manager).action_start()
        request.with_user(self.user_technician).write({'odometer_reading': 12345})
        self.assertEqual(request.odometer_reading, 12345)

    def test_technician_cannot_edit_report_while_new(self):
        request = self._make_request(employee_user=self.user_employee)
        with self.assertRaises(UserError):
            request.with_user(self.user_technician).write({'odometer_reading': 12345})

    def test_technician_cannot_edit_report_once_completed(self):
        request = self._make_request(employee_user=self.user_employee)
        request.with_user(self.user_manager).action_start()
        request.with_user(self.user_manager).action_complete()
        with self.assertRaises(UserError):
            request.with_user(self.user_technician).write({'odometer_reading': 12345})

    def test_employee_cannot_edit_report_fields(self):
        request = self._make_request(employee_user=self.user_employee)
        request.with_user(self.user_manager).action_start()
        with self.assertRaises(UserError):
            request.with_user(self.user_employee).write({'odometer_reading': 12345})

    def test_manager_can_also_edit_report_while_in_progress(self):
        request = self._make_request(employee_user=self.user_employee)
        request.with_user(self.user_manager).action_start()
        request.with_user(self.user_manager).write({'parts_cost': 250.0})
        self.assertEqual(request.parts_cost, 250.0)

    # ------------------------------------------------------------------
    # Own-request editing
    # ------------------------------------------------------------------
    def test_employee_can_edit_own_request_while_new(self):
        request = self._make_request(employee_user=self.user_employee)
        request.with_user(self.user_employee).write({'description': 'Updated description'})
        self.assertEqual(request.description, 'Updated description')

    def test_employee_cannot_edit_own_request_once_started(self):
        request = self._make_request(employee_user=self.user_employee)
        request.with_user(self.user_manager).action_start()
        with self.assertRaises(UserError):
            request.with_user(self.user_employee).write({'description': 'Changed after start'})

    def test_employee_cannot_edit_other_employee_request(self):
        request = self._make_request(employee_user=self.user_employee)
        with self.assertRaises(UserError):
            request.with_user(self.user_other).write({'description': 'Hijacked'})

    def test_employee_id_is_immutable(self):
        request = self._make_request(employee_user=self.user_employee)
        with self.assertRaises(UserError):
            request.with_user(self.user_employee).write({'employee_id': self.other_employee.id})

    # ------------------------------------------------------------------
    # Record rule visibility
    # ------------------------------------------------------------------
    def test_employee_only_sees_own_requests(self):
        own = self._make_request(employee_user=self.user_employee)
        other = self._make_request(employee_user=self.user_other)
        visible = self.env['ksw.workshop.request'].with_user(self.user_employee).search([
            ('id', 'in', (own | other).ids),
        ])
        self.assertEqual(visible, own)

    def test_manager_sees_all_requests(self):
        own = self._make_request(employee_user=self.user_employee)
        other = self._make_request(employee_user=self.user_other)
        visible = self.env['ksw.workshop.request'].with_user(self.user_manager).search([
            ('id', 'in', (own | other).ids),
        ])
        self.assertEqual(visible, own | other)

    def test_technician_sees_in_progress_not_new(self):
        new_request = self._make_request(employee_user=self.user_employee)
        in_progress_request = self._make_request(employee_user=self.user_other)
        in_progress_request.with_user(self.user_manager).action_start()
        visible = self.env['ksw.workshop.request'].with_user(self.user_technician).search([
            ('id', 'in', (new_request | in_progress_request).ids),
        ])
        self.assertEqual(visible, in_progress_request)

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------
    def test_manager_notified_on_create(self):
        request = self._make_request(employee_user=self.user_employee)
        notified = request.message_ids.filtered(
            lambda m: self.user_manager.partner_id in m.partner_ids
        )
        self.assertTrue(notified)

    def test_requester_notified_on_completion(self):
        request = self._make_request(employee_user=self.user_employee)
        request.with_user(self.user_manager).action_start()
        existing_ids = request.message_ids.ids
        request.with_user(self.user_manager).action_complete()
        new_msgs = request.message_ids.filtered(lambda m: m.id not in existing_ids)
        notified = new_msgs.filtered(lambda m: self.user_employee.partner_id in m.partner_ids)
        self.assertTrue(notified)

    def test_requester_notified_on_rejection(self):
        request = self._make_request(employee_user=self.user_employee)
        request.with_user(self.user_manager).write({'rejection_reason': 'Not needed'})
        existing_ids = request.message_ids.ids
        request.with_user(self.user_manager).action_reject()
        new_msgs = request.message_ids.filtered(lambda m: m.id not in existing_ids)
        notified = new_msgs.filtered(lambda m: self.user_employee.partner_id in m.partner_ids)
        self.assertTrue(notified)
