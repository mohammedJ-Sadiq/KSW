from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestVehicleCustody(TransactionCase):
    """The three HR-12 flows: Hand In (with a release), Receive (with a
    transfer) and Driver Change (inside a department)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Department = cls.env['hr.department']
        cls.dept_a = Department.create({'name': 'VC Alpha', 'x_fleet_holder': True})
        cls.dept_b = Department.create({'name': 'VC Bravo', 'x_fleet_holder': True})
        Users = cls.env['res.users'].with_context(no_reset_password=True)

        def _user(login, group, departments=()):
            user = Users.create({'name': login, 'login': login, 'email': f'{login}@vc.test',
                                 'group_ids': [(6, 0, [cls.env.ref(group).id])],
                                 'x_fleet_department_ids': [(6, 0, [d.id for d in departments])]})
            cls.env['hr.employee'].create({'name': login, 'user_id': user.id})
            return user

        cls.user_a = _user('vc_user_a', 'KSW_fleet_allocation.group_fleet_allocation_user', [cls.dept_a])
        cls.user_b = _user('vc_user_b', 'KSW_fleet_allocation.group_fleet_allocation_user', [cls.dept_b])
        cls.gm = _user('vc_gm', 'KSW_fleet_allocation.group_fleet_allocation_gm')
        cls.hr = _user('vc_hr', 'KSW_vehicle_custody.group_custody_hr')
        cls.accounting = _user('vc_acc', 'KSW_vehicle_custody.group_custody_accounting')
        cls.workshop = _user('vc_ws', 'KSW_workshop.group_workshop_manager')
        Employee = cls.env['hr.employee']
        cls.ahmed = Employee.create({'name': 'VC Ahmed', 'identification_id': '2400000001',
                                     'mobile_phone': '0500000001', 'department_id': cls.dept_a.id})
        cls.khalid = Employee.create({'name': 'VC Khalid', 'identification_id': '2400000002',
                                      'department_id': cls.dept_a.id})
        cls.truck = cls.env['ksw.fleet.vehicle'].create({'name': 'VC-IS1', 'vehicle_type': 'isuzu',
                                                         'x_department_id': cls.dept_a.id})
        cls.truck._ksw_set_custodian(cls.ahmed)

    # helpers ----------------------------------------------------------
    def _release(self, decision, need=None, **vals):
        """Supervisor releases the truck; the GM decides at once (no HR step
        on an intent). A transfer goes to an open request of Bravo."""
        request = self.env['ksw.fleet.request'].with_user(self.user_a).create({
            'request_type': 'release', 'department_id': self.dept_a.id, 'reason': 'Contract ended',
            'release_line_ids': [(0, 0, {'vehicle_id': self.truck.id, 'note': 'Not needed'})]})
        request.action_submit()
        self.assertEqual(request.state, 'submitted')
        self.assertFalse(request.sudo().x_custody_request_ids)  # nothing before the decision
        if decision == 'transfer':
            addition = self.env['ksw.fleet.request'].with_user(self.user_b).create({
                'request_type': 'addition', 'department_id': self.dept_b.id, 'reason': 'More work',
                'addition_line_ids': [(0, 0, {'vehicle_type': 'isuzu', 'quantity': 1, 'need': need or 'vehicle'})]})
            addition.action_submit()
            vals['addition_line_id'] = addition.addition_line_ids.id
        self.env['ksw.fleet.release.decision.wizard'].with_user(self.gm).create(dict(
            {'release_line_ids': [(6, 0, request.release_line_ids.ids)], 'decision': decision}, **vals)
        ).action_confirm()
        return request, request.sudo().x_custody_request_ids

    def _scan(self, form):
        scan = self.env['ir.attachment'].create({'name': 'hr12.pdf', 'raw': b'%PDF-1.4 signed'})
        form.sudo().write({'signed_attachment_ids': [(6, 0, scan.ids)]})

    def _workshop_done(self, form):
        form.sudo().inspection_datetime = '2026-11-12 08:00:00'
        form.with_user(self.workshop).action_workshop_done()

    def _sign(self, form):
        self._scan(form)
        form.action_mark_signed()

    # Flow 1: hand-in, workshop and HR in parallel ----------------------
    def test_hand_in_runs_workshop_and_hr_in_parallel(self):
        request, form = self._release('holding')
        self.assertEqual((form.kind, form.state), ('hand_in', 'workshop'))
        self.assertEqual((form.previous_name, form.previous_iqama, form.previous_phone),
                         ('VC Ahmed', '2400000001', '0500000001'))
        self.assertEqual(form.supervisor_id, self.user_a.employee_id)
        self.assertEqual(form.workshop_request_id, self.truck.x_checkup_request_id)
        pending = [('x_is_pending_my_action', '=', True)]
        Form = self.env['ksw.vehicle.custody.request']
        self.assertIn(form, Form.with_user(self.hr).search(pending))
        self.assertIn(form, Form.with_user(self.workshop).search(pending))
        checkup = form.workshop_request_id.sudo()
        checkup.state = 'in_progress'
        with self.assertRaises(UserError):  # not before the signed HR-12
            checkup.with_user(self.workshop).action_complete()
        form.inspection_line_ids[:1].sudo().write({'depreciation': 20, 'value': 150})
        self._workshop_done(form)  # workshop first: still waiting for HR
        self.assertEqual((form.state, form.workshop_done, form.hr_done), ('workshop', True, False))
        self.assertNotIn(form, Form.with_user(self.workshop).search(pending))
        self.assertIn(form, Form.with_user(self.hr).search(pending))
        form.with_user(self.hr).action_hr_check()
        self.assertEqual(form.state, 'done')
        self.assertFalse(self.truck.x_custodian_id)  # at the workshop: nobody's
        told = form.message_ids.filtered(lambda m: self.accounting.partner_id in m.partner_ids)
        self.assertTrue(told)
        self.assertIn('150.00', told[0].body)
        self.assertNotIn(self.hr.partner_id, told[0].partner_ids)  # HR was a step, not a copy
        with self.assertRaises(UserError):
            form.action_mark_signed()  # no scan
        self._sign(form)
        with self.assertRaises(UserError):  # locked
            form.with_user(self.hr).write({'admin_remarks': 'x'})
        checkup.with_user(self.workshop).action_complete()
        self.assertEqual(checkup.state, 'completed')

    def test_keep_opens_no_form(self):
        request, form = self._release('keep', note='Still needed')
        self.assertFalse(form)
        self.assertEqual(self.truck.x_custodian_id, self.ahmed)

    # Flow 2: receive, HR before the workshop; the handover is the arrival
    def test_receive_hr_then_workshop_handover_is_arrival(self):
        request, hand_in = self._release('transfer')
        hand_in.inspection_line_ids[:1].sudo().write({'value': 75, 'remark': 'Worn'})
        hand_in.with_user(self.hr).action_hr_check()
        self._workshop_done(hand_in)
        self._sign(hand_in)
        checkup = hand_in.workshop_request_id.sudo()
        checkup.state = 'in_progress'
        checkup.with_user(self.workshop).action_complete()
        move = self.truck.x_pending_move_id
        form = move.sudo().x_custody_request_ids
        self.assertEqual((form.kind, form.state, form.department_id), ('receive', 'draft', self.dept_b))
        self.assertEqual(form.inspection_total, 75)  # the workshop's inspection carried over
        self.assertFalse(self.env['ksw.vehicle.custody.request'].with_user(self.user_a).search([('id', '=', form.id)]))
        form.with_user(self.user_b).write({'new_employee_id': self.khalid.id})
        self.assertEqual(form.new_iqama, '2400000002')
        form.with_user(self.user_b).action_submit()
        self.assertEqual(form.state, 'hr')
        with self.assertRaises(UserError):  # the workshop cannot hand over before HR
            self._workshop_done(form)
        with self.assertRaises(UserError):  # no licence recorded
            form.with_user(self.hr).action_hr_check()
        form.with_user(self.hr).write({'license_type': 'Heavy', 'license_expiry': '2020-01-01'})
        with self.assertRaises(UserError):  # expired
            form.with_user(self.hr).action_hr_check()
        form.with_user(self.hr).write({'license_expiry': '2030-01-01'})
        form.with_user(self.hr).action_hr_check()
        self.assertEqual(form.state, 'workshop')
        self._workshop_done(form)
        self.assertEqual(form.state, 'done')
        self.assertEqual(move.state, 'done')  # arrival: no separate confirmation
        self.assertEqual(self.truck.x_department_id, self.dept_b)
        self.assertEqual(self.truck.x_custodian_id, self.khalid)
        self.assertEqual(self.truck.driver_id, self.khalid)

    # Flow 3: driver change, workshop and HR in parallel, no GM -----------
    def test_driver_change_route(self):
        Form = self.env['ksw.vehicle.custody.request']
        with self.assertRaises(UserError):  # hand-ins are not raised by hand
            Form.with_user(self.user_a).create({'kind': 'hand_in', 'vehicle_id': self.truck.id})
        with self.assertRaises(UserError):  # not his department's vehicle
            Form.with_user(self.user_b).create({'kind': 'driver_change', 'vehicle_id': self.truck.id})
        form = Form.with_user(self.user_a).create({
            'kind': 'driver_change', 'vehicle_id': self.truck.id, 'new_employee_id': self.khalid.id,
            'change_reason': 'vacation'})
        self.assertEqual((form.previous_employee_id, form.department_id), (self.ahmed, self.dept_a))
        form.with_user(self.user_a).action_submit()
        self.assertEqual(form.state, 'workshop')
        self.assertEqual(form.workshop_request_id.vehicle_id, self.truck)
        with self.assertRaises(UserError):  # HR's part needs the new user's licence
            form.with_user(self.hr).action_hr_check()
        form.with_user(self.hr).write({'license_type': 'Light', 'license_expiry': '2030-01-01'})
        form.with_user(self.hr).action_hr_check()
        self.assertEqual(form.state, 'workshop')  # still the workshop's part
        self._workshop_done(form)
        self.assertEqual(form.state, 'done')
        self.assertEqual(self.truck.x_custodian_id, self.khalid)
        history = self.truck.x_custody_ids.sorted('id')
        self.assertEqual(history.mapped('employee_id'), self.ahmed | self.khalid)
        self.assertEqual(history[-1].source, form.name)
        self.assertEqual(self.truck.x_department_id, self.dept_a)

    def test_driver_change_refused(self):
        form = self.env['ksw.vehicle.custody.request'].with_user(self.user_a).create({
            'kind': 'driver_change', 'vehicle_id': self.truck.id, 'new_employee_id': self.khalid.id,
            'change_reason': 'suspension'})
        form.with_user(self.user_a).action_submit()
        with self.assertRaises(UserError):  # the supervisor cannot refuse his own
            form.with_user(self.user_a)._refuse('no')
        self.env['ksw.vehicle.custody.refuse.wizard'].with_user(self.hr).create(
            {'request_id': form.id, 'reason': 'Licence not valid'}).action_confirm()
        self.assertEqual(form.state, 'refused')
        self.assertEqual(self.truck.x_custodian_id, self.ahmed)

    def test_hr12_prints_both_boxes_for_a_driver_change(self):
        form = self.env['ksw.vehicle.custody.request'].with_user(self.user_a).create({
            'kind': 'driver_change', 'vehicle_id': self.truck.id, 'new_employee_id': self.khalid.id,
            'change_reason': 'vacation'})
        html = self.env['ir.actions.report']._render_qweb_html(
            'KSW_vehicle_custody.report_vehicle_custody', form.ids)[0].decode()
        self.assertEqual(html.count('☑'), 3)  # handover, received, vacation
        self.assertIn('Vehicle Inspection Form', html)
        self.assertIn('VC Khalid', html)

    def test_the_intent_goes_straight_to_the_gm(self):
        request = self.env['ksw.fleet.request'].with_user(self.user_a).create({
            'request_type': 'release', 'department_id': self.dept_a.id, 'reason': 'x',
            'release_line_ids': [(0, 0, {'vehicle_id': self.truck.id})]})
        request.action_submit()
        pending = [('x_is_pending_my_action', '=', True)]
        Requests = self.env['ksw.fleet.request']
        self.assertIn(request, Requests.with_user(self.gm).search(pending))
        self.assertNotIn(request, Requests.with_user(self.hr).search(pending))
