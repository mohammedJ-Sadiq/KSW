from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestFleetAllocation(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Department = cls.env['hr.department']
        cls.dept_a = Department.create({'name': 'FA Alpha', 'x_fleet_holder': True})
        cls.dept_a_sub = Department.create({'name': 'FA Alpha Sub', 'parent_id': cls.dept_a.id})
        cls.dept_b = Department.create({'name': 'FA Bravo', 'x_fleet_holder': True})
        Users = cls.env['res.users'].with_context(no_reset_password=True)

        def _user(login, group, departments=()):
            user = Users.create({
                'name': login, 'login': login, 'email': f'{login}@fa.test',
                'group_ids': [(6, 0, [cls.env.ref(group).id])],
                'x_fleet_department_ids': [(6, 0, [d.id for d in departments])]})
            cls.env['hr.employee'].create({'name': login, 'user_id': user.id})
            return user

        cls.user_a = _user('fa_user_a', 'KSW_fleet_allocation.group_fleet_allocation_user', [cls.dept_a])
        cls.user_b = _user('fa_user_b', 'KSW_fleet_allocation.group_fleet_allocation_user', [cls.dept_b])
        cls.gm = _user('fa_gm', 'KSW_fleet_allocation.group_fleet_allocation_gm')
        # With Vehicle Custody installed (always, in KSW) a release goes
        # through HR and a vehicle in use through a workshop checkup.
        cls.hr = _user('fa_hr', 'KSW_vehicle_custody.group_custody_hr')
        cls.workshop = _user('fa_workshop', 'KSW_workshop.group_workshop_manager')

        Vehicle = cls.env['ksw.fleet.vehicle']
        cls.isuzu_a1 = Vehicle.create({'name': 'FA-IS1', 'vehicle_type': 'isuzu', 'x_department_id': cls.dept_a.id})
        cls.isuzu_a2 = Vehicle.create({'name': 'FA-IS2', 'vehicle_type': 'isuzu', 'x_department_id': cls.dept_a.id})
        cls.trailer_b = Vehicle.create({'name': 'FA-T1', 'vehicle_type': 'trailer', 'x_department_id': cls.dept_b.id})

    # helpers ----------------------------------------------------------
    def _release(self, user, department, vehicles, hr_review=True):
        request = self.env['ksw.fleet.request'].with_user(user).create({
            'request_type': 'release', 'department_id': department.id, 'reason': 'Not needed',
            'release_line_ids': [(0, 0, {'vehicle_id': v.id}) for v in vehicles]})
        request.action_submit()
        if hr_review and request.state == 'hr_review':
            request.with_user(self.hr).action_hr_send_to_gm()
        return request

    def _addition(self, user, department, vehicle_type, quantity):
        request = self.env['ksw.fleet.request'].with_user(user).create({
            'request_type': 'addition', 'department_id': department.id, 'reason': 'New contract',
            'addition_line_ids': [(0, 0, {'vehicle_type': vehicle_type, 'quantity': quantity})]})
        request.action_submit()
        return request

    def _decide_release(self, lines, **vals):
        wizard = self.env['ksw.fleet.release.decision.wizard'].with_user(self.gm).create(
            dict({'release_line_ids': [(6, 0, lines.ids)]}, **vals))
        wizard.action_confirm()

    def _allocate(self, line, **vals):
        wizard = self.env['ksw.fleet.allocate.wizard'].with_user(self.gm).create(
            dict({'addition_line_id': line.id}, **vals))
        wizard.action_confirm()

    def _attach_scan(self, form):
        scan = self.env['ir.attachment'].create({'name': 'hr12.pdf', 'raw': b'%PDF-1.4 signed'})
        form.sudo().write({'signed_attachment_ids': [(6, 0, scan.ids)]})

    def _finish_custody(self, forms):
        """Drive HR-12 forms to Signed: HR, the workshop, the signed scan."""
        for form in forms.filtered(lambda f: f.state == 'hr'):
            if form.new_employee_id:
                form.sudo().write({'license_type': 'Heavy', 'license_expiry': '2030-01-01'})
            form.with_user(self.hr).action_hr_approve()
        for form in forms.filtered(lambda f: f.state == 'workshop'):
            form.sudo().inspection_datetime = '2026-11-12 08:00:00'
            form.with_user(self.workshop).action_workshop_done()
        for form in forms.filtered(lambda f: f.state == 'done'):
            self._attach_scan(form)
            form.with_user(self.workshop).action_mark_signed()

    def _finish_checkup(self, vehicle):
        """The workshop checks the vehicle: its hand-in signed, the checkup
        completed (which starts a reserved transfer)."""
        request = self.env['ksw.workshop.request'].sudo().search(
            [('vehicle_id', '=', vehicle.id), ('state', '!=', 'completed')], order='id desc', limit=1)
        self._finish_custody(request.x_custody_request_ids)
        request.state = 'in_progress'
        request.with_user(self.workshop).action_complete()

    def _transfer(self, lines, target):
        """GM transfers released vehicles: checkup first, then on their way."""
        self._decide_release(lines, decision='transfer', target_department_id=target.id)
        for vehicle in lines.vehicle_id:
            self._finish_checkup(vehicle)

    def _receive(self, moves, user, **vals):
        # The receiving department names the new driver on HR-12; HR and the
        # workshop approve it; then arrival is confirmed.
        for form in moves.sudo().x_custody_request_ids.filtered(lambda f: f.state == 'draft'):
            form.with_user(user).write({'new_employee_id': user.employee_id.id})
            form.with_user(user).action_submit()
        self._finish_custody(moves.sudo().x_custody_request_ids)
        action = moves.with_user(user).action_receive()
        wizard = self.env['ksw.fleet.receive.wizard'].with_user(user).with_context(**action['context']).create({})
        if vals:
            wizard.line_ids.write(vals)
        wizard.action_confirm()

    # tests ------------------------------------------------------------
    def test_starting_assignment_is_logged(self):
        self.assertEqual(self.isuzu_a1.x_allocation_state, 'assigned')
        self.assertEqual(self.isuzu_a1.x_move_ids.kind, 'initial')
        self.assertEqual(self.isuzu_a1.x_move_ids.to_department_id, self.dept_a)

    def test_release_transferred_to_requesting_department(self):
        release = self._release(self.user_a, self.dept_a, self.isuzu_a1)
        addition = self._addition(self.user_b, self.dept_b, 'isuzu', 1)
        self.assertTrue(self.isuzu_a1.x_is_released)
        self._decide_release(release.release_line_ids, decision='transfer',
                             target_department_id=self.dept_b.id)
        # A vehicle in use goes through the workshop first, reserved for Bravo.
        self.assertEqual(self.isuzu_a1.x_allocation_state, 'holding')
        self.assertEqual(self.isuzu_a1.x_reserved_department_id, self.dept_b)
        self.assertEqual(release.release_line_ids.status, 'checkup')
        self.assertEqual(addition.addition_line_ids.internal_quantity, 1)
        self._finish_checkup(self.isuzu_a1)
        # Not moved until the receiving side confirms.
        self.assertEqual(self.isuzu_a1.x_allocation_state, 'in_transfer')
        self.assertFalse(self.isuzu_a1.x_department_id)
        move = self.isuzu_a1.x_pending_move_id
        with self.assertRaises(UserError):
            move.with_user(self.user_a).action_receive()
        with self.assertRaises(UserError):  # not even the GM
            move.with_user(self.gm).action_receive()
        self.assertFalse(move.with_user(self.gm).x_can_receive)
        self._receive(move, self.user_b)
        self.assertEqual(self.isuzu_a1.x_department_id, self.dept_b)
        self.assertEqual(self.isuzu_a1.x_allocation_state, 'assigned')
        self.assertEqual(release.state, 'done')
        self.assertEqual(addition.state, 'done')
        self.assertEqual(addition.addition_line_ids.internal_quantity, 1)

    def test_release_to_workshop_goes_to_holding(self):
        release = self._release(self.user_a, self.dept_a, self.isuzu_a1)
        self._decide_release(release.release_line_ids, decision='holding',
                             workshop_description='Engine check')
        self.assertEqual(self.isuzu_a1.x_allocation_state, 'holding')
        self.assertFalse(self.isuzu_a1.x_department_id)
        workshop = self.isuzu_a1.x_move_ids.filtered(lambda m: m.kind == 'holding').workshop_request_id
        self.assertTrue(workshop)
        self.assertEqual(workshop.vehicle_id, self.isuzu_a1)
        self.assertEqual(release.state, 'done')

    def test_keep_closes_release_without_moving(self):
        release = self._release(self.user_a, self.dept_a, self.isuzu_a1)
        self._decide_release(release.release_line_ids, decision='keep', note='Still needed')
        self.assertEqual(self.isuzu_a1.x_department_id, self.dept_a)
        self.assertEqual(release.release_line_ids.status, 'kept')
        self.assertEqual(release.state, 'done')

    def test_addition_partly_internal_external_refused(self):
        # One in the pool to give internally.
        self.isuzu_a2.write({'x_department_id': False})
        addition = self._addition(self.user_b, self.dept_b, 'isuzu', 3)
        line = addition.addition_line_ids
        self._allocate(line, mode='internal', vehicle_ids=[(6, 0, self.isuzu_a2.ids)])
        self._allocate(line, mode='external', decide_quantity=1, external_method='rent')
        with self.assertRaises(UserError):  # refusing needs a reason
            self._allocate(line, mode='refuse', decide_quantity=1)
        self._allocate(line, mode='refuse', decide_quantity=1, note='Budget')
        self.assertEqual(line.open_quantity, 0)
        self.assertEqual(line.status, 'waiting_external')
        self.assertEqual(addition.state, 'submitted')
        # The rented one arrives: registered without department, then allocated.
        rented = self.env['ksw.fleet.vehicle'].create({'name': 'FA-RENT', 'vehicle_type': 'isuzu'})
        self._allocate(line, mode='external_arrival', vehicle_ids=[(6, 0, rented.ids)])
        for move in line.move_ids:
            self._receive(move, self.user_b)
        self.assertEqual(line.status, 'done')
        self.assertEqual(addition.state, 'done')
        self.assertEqual(rented.x_department_id, self.dept_b)

    def test_cannot_give_more_than_open(self):
        self.isuzu_a1.write({'x_department_id': False})
        self.isuzu_a2.write({'x_department_id': False})
        addition = self._addition(self.user_b, self.dept_b, 'isuzu', 1)
        with self.assertRaises(UserError):
            self._allocate(addition.addition_line_ids, mode='internal',
                           vehicle_ids=[(6, 0, (self.isuzu_a1 | self.isuzu_a2).ids)])

    def test_cancel_transfer_returns_vehicle_to_the_pool(self):
        """The vehicle already left its department for the workshop: a
        cancelled transfer leaves it in the pool, its custody form cancelled."""
        release = self._release(self.user_a, self.dept_a, self.isuzu_a1)
        self._transfer(release.release_line_ids, self.dept_b)
        move = self.isuzu_a1.x_pending_move_id
        with self.assertRaises(UserError):
            move.with_user(self.user_a).action_cancel()
        move.with_user(self.gm).action_cancel()
        self.assertEqual(self.isuzu_a1.x_allocation_state, 'holding')
        self.assertEqual(release.release_line_ids.status, 'holding')
        self.assertEqual(move.sudo().x_custody_request_ids.state, 'cancelled')

    def test_department_scope(self):
        request_b = self._addition(self.user_b, self.dept_b, 'trailer', 1)
        self.assertNotIn(request_b, self.env['ksw.fleet.request'].with_user(self.user_a).search([]))
        with self.assertRaises(AccessError):
            request_b.with_user(self.user_a).read(['name'])
        with self.assertRaises(UserError):
            self.env['ksw.fleet.request'].with_user(self.user_a).create({
                'request_type': 'addition', 'department_id': self.dept_b.id, 'reason': 'x'})
        # Sub-departments are covered by the parent.
        self.assertIn(self.dept_a_sub.id, self.user_a._ksw_fleet_department_ids())

    def test_cannot_release_another_departments_vehicle(self):
        with self.assertRaises(UserError):
            self._release(self.user_a, self.dept_a, self.trailer_b)

    def test_vehicle_released_once(self):
        self._release(self.user_a, self.dept_a, self.isuzu_a1)
        with self.assertRaises(UserError):
            self._release(self.user_a, self.dept_a, self.isuzu_a1)

    def test_requester_cannot_decide_or_move(self):
        release = self._release(self.user_a, self.dept_a, self.isuzu_a1)
        with self.assertRaises(UserError):
            release.release_line_ids.with_user(self.user_a).action_open_decision()
        with self.assertRaises(AccessError):
            self.env['ksw.fleet.release.decision.wizard'].with_user(self.user_a).create(
                {'release_line_ids': [(6, 0, release.release_line_ids.ids)]})
        with self.assertRaises(UserError):
            release.release_line_ids.with_user(self.user_a).write({'decision': 'keep'})
        with self.assertRaises(UserError):
            self.isuzu_a1.with_user(self.user_a).write({'x_department_id': self.dept_b.id})
        with self.assertRaises(UserError):
            release.with_user(self.user_a).write({'reason': 'changed'})

    def test_cancel_refused_once_decided(self):
        release = self._release(self.user_a, self.dept_a, self.isuzu_a1 | self.isuzu_a2)
        self._decide_release(release.release_line_ids[0], decision='keep', note='Needed')
        with self.assertRaises(UserError):
            release.with_user(self.user_a).action_cancel()

    def test_seed_from_drivers(self):
        driver = self.env['hr.employee'].create({'name': 'FA Driver', 'department_id': self.dept_a_sub.id})
        vehicle = self.env['ksw.fleet.vehicle'].create({'name': 'FA-SEED', 'vehicle_type': 'isuzu',
                                                       'driver_id': driver.id})
        if 'x_bas_driver_id' in vehicle._fields:
            vehicle.x_bas_driver_id = False
        self.env['ksw.fleet.vehicle']._ksw_fleet_seed_from_drivers()
        # Placed in the vehicle-holding department above the driver's.
        self.assertEqual(vehicle.x_department_id, self.dept_a)

    def test_naming_departments_grants_the_role(self):
        user = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'fa_plain', 'login': 'fa_plain', 'group_ids': [(6, 0, [self.env.ref('base.group_user').id])]})
        role = self.env.ref('KSW_fleet_allocation.group_fleet_allocation_user')
        self.assertNotIn(role, user.all_group_ids)
        user.write({'x_fleet_department_ids': [(6, 0, self.dept_a.ids)]})
        self.assertIn(role, user.all_group_ids)

    def test_bulk_release_partial(self):
        """Tick several, untick one: the decision applies to the ticked ones,
        is counted against the receiver's open request, and posts once."""
        trailer_a = self.env['ksw.fleet.vehicle'].create(
            {'name': 'FA-T2', 'vehicle_type': 'trailer', 'x_department_id': self.dept_a.id})
        release = self._release(self.user_a, self.dept_a, self.isuzu_a1 | self.isuzu_a2 | trailer_a)
        addition = self._addition(self.user_b, self.dept_b, 'isuzu', 1)
        action = release.with_user(self.gm).action_decide_release_lines()
        self.assertEqual(len(action['context']['default_release_line_ids'][0][2]), 3)
        lines = release.release_line_ids.filtered(lambda l: l.vehicle_id != trailer_a)
        before = len(release.message_ids)
        self._decide_release(lines, decision='transfer', target_department_id=self.dept_b.id)
        self.assertEqual(len(release.message_ids) - before, 1)
        # Both to the workshop first, reserved for Bravo.
        self.assertEqual((self.isuzu_a1 | self.isuzu_a2).mapped('x_allocation_state'), ['holding'] * 2)
        self.assertEqual((self.isuzu_a1 | self.isuzu_a2).x_reserved_department_id, self.dept_b)
        self.assertEqual(trailer_a.x_allocation_state, 'assigned')
        # One isuzu answers the request for one; the other goes unlinked.
        self.assertEqual(addition.addition_line_ids.internal_quantity, 1)
        self.assertEqual(release.release_line_ids.filtered(lambda l: l.vehicle_id == trailer_a).status, 'pending')
        # Bravo can read the release sending it vehicles, but not Alpha's other line.
        # search, not release.release_line_ids: that x2many is cached from
        # the admin's reads and would show every line (gotcha #201).
        visible = self.env['ksw.fleet.release.line'].with_user(self.user_b).search(
            [('request_id', '=', release.id)])
        release.with_user(self.user_b).read(['name', 'reason'])
        self.assertEqual(visible.vehicle_id, self.isuzu_a1 | self.isuzu_a2)
        with self.assertRaises(UserError):
            self._decide_release(lines, decision='keep')

    def test_vehicles_menu_scoped(self):
        domain = [('x_is_own_fleet', '=', True), ('x_in_my_scope', '=', True)]
        seen_a = self.env['ksw.fleet.vehicle'].with_user(self.user_a).search(domain)
        self.assertIn(self.isuzu_a1, seen_a)
        self.assertNotIn(self.trailer_b, seen_a)
        self.assertIn(self.trailer_b, self.env['ksw.fleet.vehicle'].with_user(self.gm).search(domain))

    def _todo(self, request, xmlid, user):
        return request.activity_ids.filtered(
            lambda a: a.activity_type_id == self.env.ref(xmlid) and a.user_id == user)

    def test_activities_follow_the_work(self):
        """GM gets a Decide to-do on submit, the receiver a Confirm Arrival on
        transfer; both disappear once the work is done."""
        decide, receive = 'KSW_fleet_allocation.mail_activity_fleet_decide', \
            'KSW_fleet_allocation.mail_activity_fleet_receive'
        release = self._release(self.user_a, self.dept_a, self.isuzu_a1)
        addition = self._addition(self.user_b, self.dept_b, 'isuzu', 1)
        self.assertTrue(self._todo(release, decide, self.gm))
        self._transfer(release.release_line_ids, self.dept_b)
        self.assertFalse(self._todo(release, decide, self.gm))
        # Counted against Bravo's request, so the to-do sits on Bravo's own request.
        self.assertTrue(self._todo(addition, receive, self.user_b))
        self.assertFalse(self._todo(addition, decide, self.gm))
        self._receive(self.isuzu_a1.x_pending_move_id, self.user_b)
        self.assertFalse(self._todo(addition, receive, self.user_b))

    def test_cancelled_request_clears_gm_todo(self):
        release = self._release(self.user_a, self.dept_a, self.isuzu_a1)
        hand_in = release.sudo().x_custody_request_ids
        release.with_user(self.user_a).action_cancel()
        self.assertFalse(self._todo(release, 'KSW_fleet_allocation.mail_activity_fleet_decide', self.gm))
        self.assertEqual(hand_in.state, 'cancelled')

    def test_release_selected_from_vehicle_list(self):
        action = (self.isuzu_a1 | self.isuzu_a2).with_user(self.user_a).action_ksw_fleet_release_selected()
        self.assertEqual(action['context']['default_department_id'], self.dept_a.id)
        self.assertEqual(len(action['context']['default_release_line_ids']), 2)
        with self.assertRaises(UserError):  # not his
            self.trailer_b.with_user(self.user_a).action_ksw_fleet_release_selected()
        with self.assertRaises(UserError):  # two departments at once
            (self.isuzu_a1 | self.trailer_b).with_user(self.gm).action_ksw_fleet_release_selected()
        request = self.env['ksw.fleet.request'].with_user(self.user_a).with_context(
            **action['context']).create({'reason': 'Contract ended'})
        self.assertEqual(request.release_line_ids.vehicle_id, self.isuzu_a1 | self.isuzu_a2)

    def test_to_receive_lists_only_incoming(self):
        release = self._release(self.user_a, self.dept_a, self.isuzu_a1)
        self._transfer(release.release_line_ids, self.dept_b)
        domain = [('state', '=', 'in_transfer'), ('x_is_incoming', '=', True)]
        Move = self.env['ksw.fleet.move']
        self.assertTrue(Move.with_user(self.user_b).search(domain))
        self.assertFalse(Move.with_user(self.user_a).search(domain))
        self.assertFalse(Move.with_user(self.gm).search(domain))

    def test_overview_shows_own_departments(self):
        domain = [('x_fleet_holder', '=', True), ('x_fleet_in_my_scope', '=', True)]
        Department = self.env['hr.department']
        self.assertEqual(Department.with_user(self.user_a).search(domain) & (self.dept_a | self.dept_b), self.dept_a)
        self.assertEqual(Department.with_user(self.gm).search(domain) & (self.dept_a | self.dept_b),
                         self.dept_a | self.dept_b)
        self.assertEqual(self.dept_a.with_user(self.user_a).x_fleet_vehicle_count, 2)

    def test_keep_needs_a_reason(self):
        release = self._release(self.user_a, self.dept_a, self.isuzu_a1)
        with self.assertRaises(UserError):
            self._decide_release(release.release_line_ids, decision='keep')

    def test_held_vehicle_waits_for_its_checkup(self):
        release = self._release(self.user_a, self.dept_a, self.isuzu_a1)
        self._decide_release(release.release_line_ids, decision='holding')
        checkup = self.isuzu_a1.x_checkup_request_id
        self.assertTrue(self.isuzu_a1.x_checkup_open)
        addition = self._addition(self.user_b, self.dept_b, 'isuzu', 1)
        line = addition.addition_line_ids
        wizard = self.env['ksw.fleet.allocate.wizard'].with_user(self.gm).create(
            {'addition_line_id': line.id, 'mode': 'internal'})
        self.assertNotIn(self.isuzu_a1, wizard.available_vehicle_ids)
        with self.assertRaises(UserError):  # nor through the back door
            self.isuzu_a1.sudo()._ksw_fleet_start_transfer(self.dept_b, addition_line=line.sudo())
        checkup.sudo().write({'state': 'completed'})
        self.isuzu_a1.invalidate_recordset(['x_checkup_open'])
        wizard.invalidate_recordset(['available_vehicle_ids'])
        self.assertIn(self.isuzu_a1, wizard.available_vehicle_ids)
        self._allocate(line, mode='internal', vehicle_ids=[(6, 0, self.isuzu_a1.ids)])
        self.assertEqual(self.isuzu_a1.x_allocation_state, 'in_transfer')

    def test_taking_an_unreleased_vehicle(self):
        """Needs a reason, and the department that loses it is told on a
        request it can read."""
        addition = self._addition(self.user_b, self.dept_b, 'isuzu', 1)
        line = addition.addition_line_ids
        with self.assertRaises(UserError):
            self._allocate(line, mode='internal', include_assigned=True,
                           vehicle_ids=[(6, 0, self.isuzu_a1.ids)])
        self._allocate(line, mode='internal', include_assigned=True, note='Bravo has a new contract',
                       vehicle_ids=[(6, 0, self.isuzu_a1.ids)])
        # Taken while in use: the workshop first, and HR checks its hand-in.
        self.assertEqual(self.isuzu_a1.x_reserved_department_id, self.dept_b)
        hand_in = self.isuzu_a1.x_checkup_request_id.sudo().x_custody_request_ids
        self.assertEqual((hand_in.kind, hand_in.state), ('hand_in', 'hr'))
        told = addition.message_ids.filtered(lambda m: self.user_a.partner_id in m.partner_ids)
        self.assertTrue(told)
        self.assertIn('Taken from', told[0].body)
        self.assertIn(addition, self.env['ksw.fleet.request'].with_user(self.user_a).search([]))
        with self.assertRaises(UserError):  # read only for Alpha
            addition.with_user(self.user_a).action_cancel()

    def test_handover_records_condition(self):
        release = self._release(self.user_a, self.dept_a, self.isuzu_a1)
        self._transfer(release.release_line_ids, self.dept_b)
        move = self.isuzu_a1.x_pending_move_id
        with self.assertRaises(UserError):  # damaged without saying what
            self._receive(move, self.user_b, condition='damaged')
        self.assertEqual(move.state, 'in_transfer')
        self._receive(move, self.user_b, condition='damaged', odometer=152300, note='Cracked mirror')
        self.assertEqual((move.received_condition, move.received_odometer, move.received_note),
                         ('damaged', 152300, 'Cracked mirror'))
        self.assertTrue(release.message_ids.filtered(
            lambda m: self.gm.partner_id in m.partner_ids and 'Cracked mirror' in m.body))
