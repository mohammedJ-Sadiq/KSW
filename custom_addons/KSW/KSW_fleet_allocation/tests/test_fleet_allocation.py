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
    def _release(self, user, department, vehicles):
        request = self.env['ksw.fleet.request'].with_user(user).create({
            'request_type': 'release', 'department_id': department.id, 'reason': 'Not needed',
            'release_line_ids': [(0, 0, {'vehicle_id': v.id}) for v in vehicles]})
        request.action_submit()
        return request

    def _addition(self, user, department, vehicle_type, quantity, need='vehicle'):
        request = self.env['ksw.fleet.request'].with_user(user).create({
            'request_type': 'addition', 'department_id': department.id, 'reason': 'New contract',
            'addition_line_ids': [(0, 0, {'vehicle_type': vehicle_type, 'quantity': quantity,
                                          'need': need})]})
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
        """Drive HR-12 forms to Signed: HR's documents, the workshop, the scan."""
        for form in forms.filtered(lambda f: f.state == 'hr'):
            form.sudo().write({'license_type': 'Heavy', 'license_expiry': '2030-01-01'})
            form.with_user(self.hr).action_hr_check()
        for form in forms.filtered(lambda f: f.state == 'workshop'):
            if form.kind != 'receive' and not form.hr_done:
                form.with_user(self.hr).action_hr_check()
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

    def _transfer(self, lines, addition_line):
        """GM gives released vehicles to an open request: checkup first,
        then on their way."""
        self._decide_release(lines, decision='transfer', addition_line_id=addition_line.id)
        for vehicle in lines.vehicle_id:
            self._finish_checkup(vehicle)

    def _receive(self, moves, user):
        """The receiving department names the new driver; HR checks him; the
        workshop hands the vehicle over, which is the arrival."""
        for form in moves.sudo().x_custody_request_ids.filtered(lambda f: f.state == 'draft'):
            form.with_user(user).write({'new_employee_id': user.employee_id.id})
            form.with_user(user).action_submit()
        self._finish_custody(moves.sudo().x_custody_request_ids)

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
                             addition_line_id=addition.addition_line_ids.id)
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
        # Nobody confirms arrival separately: the workshop handover does.
        self.assertFalse(move.with_user(self.user_b).x_can_receive)
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
        addition = self._addition(self.user_b, self.dept_b, 'isuzu', 1)
        self._transfer(release.release_line_ids, addition.addition_line_ids)
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
        addition = self._addition(self.user_b, self.dept_b, 'isuzu', 2)
        action = release.with_user(self.gm).action_decide_release_lines()
        self.assertEqual(len(action['context']['default_release_line_ids'][0][2]), 3)
        lines = release.release_line_ids.filtered(lambda l: l.vehicle_id != trailer_a)
        before = len(release.message_ids)
        self._decide_release(lines, decision='transfer', addition_line_id=addition.addition_line_ids.id)
        self.assertEqual(len(release.message_ids) - before, 1)
        # Both to the workshop first, reserved for Bravo.
        self.assertEqual((self.isuzu_a1 | self.isuzu_a2).mapped('x_allocation_state'), ['holding'] * 2)
        self.assertEqual((self.isuzu_a1 | self.isuzu_a2).x_reserved_department_id, self.dept_b)
        self.assertEqual(trailer_a.x_allocation_state, 'assigned')
        self.assertEqual(addition.addition_line_ids.internal_quantity, 2)
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
        """The GM gets a Decide to-do on submit; it goes once he decided."""
        decide = 'KSW_fleet_allocation.mail_activity_fleet_decide'
        release = self._release(self.user_a, self.dept_a, self.isuzu_a1)
        addition = self._addition(self.user_b, self.dept_b, 'isuzu', 1)
        self.assertTrue(self._todo(release, decide, self.gm))
        self._transfer(release.release_line_ids, addition.addition_line_ids)
        self.assertFalse(self._todo(release, decide, self.gm))
        self.assertFalse(self._todo(addition, decide, self.gm))
        # No separate "confirm arrival" to-do: the receiving department's
        # step is the new driver's HR-12.
        self.assertFalse(self._todo(addition, 'KSW_fleet_allocation.mail_activity_fleet_receive', self.user_b))

    def test_cancelled_request_clears_gm_todo(self):
        release = self._release(self.user_a, self.dept_a, self.isuzu_a1)
        self.assertFalse(release.sudo().x_custody_request_ids)  # none before the GM decides
        release.with_user(self.user_a).action_cancel()
        self.assertFalse(self._todo(release, 'KSW_fleet_allocation.mail_activity_fleet_decide', self.gm))

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
        addition = self._addition(self.user_b, self.dept_b, 'isuzu', 1)
        self._transfer(release.release_line_ids, addition.addition_line_ids)
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

    def test_no_vehicle_from_a_department_that_did_not_release(self):
        """Vehicles only come from a release or the pool."""
        addition = self._addition(self.user_b, self.dept_b, 'isuzu', 1)
        wizard = self.env['ksw.fleet.allocate.wizard'].with_user(self.gm).create(
            {'addition_line_id': addition.addition_line_ids.id, 'mode': 'internal'})
        self.assertNotIn(self.isuzu_a1, wizard.available_vehicle_ids)
        with self.assertRaises(UserError):
            self._allocate(addition.addition_line_ids, mode='internal', vehicle_ids=[(6, 0, self.isuzu_a1.ids)])

    def test_only_to_a_department_that_asked(self):
        """The GM gives a released vehicle to an open request: same type,
        a fitting need, and no more than asked."""
        trailer_a = self.env['ksw.fleet.vehicle'].create(
            {'name': 'FA-T3', 'vehicle_type': 'trailer', 'x_department_id': self.dept_a.id})
        release = self._release(self.user_a, self.dept_a, self.isuzu_a1 | self.isuzu_a2 | trailer_a)
        lines = release.release_line_ids
        isuzus = lines.filtered(lambda l: l.vehicle_type == 'isuzu')
        with self.assertRaises(UserError):  # no request chosen
            self._decide_release(isuzus, decision='transfer')
        one = self._addition(self.user_b, self.dept_b, 'isuzu', 1)
        with self.assertRaises(UserError):  # two for a need of one
            self._decide_release(isuzus, decision='transfer', addition_line_id=one.addition_line_ids.id)
        with self.assertRaises(UserError):  # a trailer for an Isuzu need
            self._decide_release(lines - isuzus, decision='transfer', addition_line_id=one.addition_line_ids.id)
        with_driver = self._addition(self.user_b, self.dept_b, 'isuzu', 1, need='with_driver')
        with self.assertRaises(UserError):  # released alone, the need wants a driver
            self._decide_release(isuzus[:1], decision='transfer', addition_line_id=with_driver.addition_line_ids.id)
        either = self._addition(self.user_b, self.dept_b, 'isuzu', 2, need='either')
        self._decide_release(isuzus, decision='transfer', addition_line_id=either.addition_line_ids.id)
        self.assertEqual((self.isuzu_a1 | self.isuzu_a2).x_reserved_department_id, self.dept_b)
        self.assertEqual(either.addition_line_ids.open_quantity, 0)

    def test_arrival_is_the_workshop_handover(self):
        release = self._release(self.user_a, self.dept_a, self.isuzu_a1)
        addition = self._addition(self.user_b, self.dept_b, 'isuzu', 1)
        self._transfer(release.release_line_ids, addition.addition_line_ids)
        move = self.isuzu_a1.x_pending_move_id
        self.assertEqual(move.state, 'in_transfer')
        self._receive(move, self.user_b)
        self.assertEqual(move.state, 'done')
        self.assertEqual(self.isuzu_a1.x_department_id, self.dept_b)
        self.assertEqual(self.isuzu_a1.x_custodian_id, self.user_b.employee_id)
        self.assertEqual(addition.state, 'done')
