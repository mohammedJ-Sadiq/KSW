from odoo.exceptions import UserError, ValidationError
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

        cls.vehicle = cls.env['ksw.fleet.vehicle'].create({'name': 'WS-152', 'vehicle_type': 'isuzu'})
        # customer_rank=1 is what makes a partner a client — the same marker
        # client_id's domain filters on. A fixture without it would not model
        # a real client.
        cls.other_client = cls.env['res.partner'].create({
            'name': 'WS Other Client', 'customer_rank': 1,
        })
        cls.other_client_vehicle = cls.env['ksw.fleet.vehicle'].create({
            'name': 'WS-153', 'vehicle_type': 'trailer', 'client_id': cls.other_client.id,
        })

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

    # ------------------------------------------------------------------
    # Client / Vehicle cascade
    # ------------------------------------------------------------------
    def test_client_defaults_to_company(self):
        request = self._make_request(employee_user=self.user_employee)
        self.assertEqual(request.client_id, self.env.company.partner_id)

    def test_vehicle_auto_clears_and_blocks_save_on_client_change_alone(self):
        # Changing client_id without also picking a new vehicle for it is
        # exactly the "saved mid-cascade" case: the compute clears the now
        # mismatched vehicle_id, and _check_vehicle_and_cash_customer then
        # correctly refuses the save until a new vehicle is picked (the
        # real form only ever reaches write() with both changes together,
        # since onchange-style computes apply before the user clicks Save).
        request = self._make_request(employee_user=self.user_employee)
        with self.assertRaises(ValidationError):
            request.with_user(self.user_manager).write({'client_id': self.other_client.id})

    def test_vehicle_domain_resets_on_client_change(self):
        request = self._make_request(employee_user=self.user_employee)
        request.with_user(self.user_manager).write({
            'client_id': self.other_client.id, 'vehicle_id': self.other_client_vehicle.id,
        })
        self.assertEqual(request.vehicle_id, self.other_client_vehicle)

    def test_vehicle_client_mismatch_rejected_via_constrains(self):
        request = self._make_request(employee_user=self.user_employee)
        with self.assertRaises(ValidationError):
            request.with_user(self.user_manager).write({
                'client_id': self.other_client.id, 'vehicle_id': self.vehicle.id,
            })

    def test_new_draft_vehicle_visible_only_on_own_request(self):
        draft_vehicle = self.env['ksw.fleet.vehicle'].with_user(self.user_employee).create({
            'name': 'WS-DRAFT-1',
            'client_id': self.env.company.partner_id.id,
            'vehicle_type': 'other',
        })
        self.assertEqual(draft_vehicle.state, 'draft')
        own_request = self._make_request(employee_user=self.user_employee, vehicle_id=draft_vehicle.id)
        self.assertEqual(own_request.vehicle_id, draft_vehicle)

        other_visible = self.env['ksw.fleet.vehicle'].with_user(self.user_employee).search([
            ('id', '=', draft_vehicle.id),
            ('client_id', '=', self.env.company.partner_id.id),
            ('vehicle_type', '=', 'other'),
            '|', ('state', '=', 'confirmed'), ('id', '=', False),
        ])
        self.assertFalse(other_visible)

    # ------------------------------------------------------------------
    # Cash Customer
    # ------------------------------------------------------------------
    def test_employee_cannot_toggle_cash_customer(self):
        request = self._make_request(employee_user=self.user_employee)
        with self.assertRaises(UserError):
            request.with_user(self.user_employee).write({'is_cash_customer': True})

    def test_manager_can_toggle_cash_customer(self):
        request = self._make_request(employee_user=self.user_employee)
        request.with_user(self.user_manager).write({
            'is_cash_customer': True,
            'x_cash_customer_name': 'Walk-in Co',
            'x_cash_vehicle_number': '999',
        })
        self.assertTrue(request.is_cash_customer)

    def test_cash_customer_bypasses_vehicle_requirement(self):
        # client_id's default (env.company.partner_id) still fires on a raw
        # create() — the onchange that clears it only runs in the
        # interactive form flow. That leftover value is harmless: the
        # constrains skip the client/vehicle checks entirely whenever
        # is_cash_customer is True. vehicle_id has no default, so it's a
        # clean way to prove the requirement was genuinely bypassed.
        request = self.env['ksw.workshop.request'].with_user(self.user_manager).create({
            'description': 'Walk-in repair',
            'is_cash_customer': True,
            'x_cash_customer_name': 'Walk-in Co',
            'x_cash_vehicle_number': '999',
        })
        self.assertFalse(request.vehicle_id)

    def test_cash_customer_requires_name_and_vehicle_number(self):
        with self.assertRaises(ValidationError):
            self.env['ksw.workshop.request'].with_user(self.user_manager).create({
                'description': 'Walk-in repair',
                'is_cash_customer': True,
            })

    def test_regular_request_requires_client_and_vehicle(self):
        with self.assertRaises(ValidationError):
            self.env['ksw.workshop.request'].with_user(self.user_manager).create({
                'description': 'Missing vehicle',
                'employee_id': self.employee.id,
                'client_id': False,
                'vehicle_id': False,
            })

    def test_employee_created_request_forces_cash_customer_false(self):
        request = self.env['ksw.workshop.request'].with_user(self.user_employee).create({
            'description': 'Battery problem',
            'vehicle_id': self.vehicle.id,
            'is_cash_customer': True,
            'x_cash_customer_name': 'Should be ignored',
            'x_cash_vehicle_number': '999',
        })
        self.assertFalse(request.is_cash_customer)
