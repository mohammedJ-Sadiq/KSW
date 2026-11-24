from datetime import date, timedelta

from odoo.exceptions import RedirectWarning, UserError, ValidationError
from odoo.tests import TransactionCase, tagged

def _register_papers(env, vehicle):
    """A company vehicle's handover papers, valid (the register as HR keeps it)."""
    for doc_type, _status in vehicle._ksw_handover_gaps().get(vehicle.id, []):
        env['ksw.document'].create({
            'type_id': doc_type.id, 'applies_to': 'vehicle', 'vehicle_id': vehicle.id,
            'number': 'P-%s-%s' % (vehicle.id, doc_type.id),
            'expiry_date': date.today() + timedelta(days=365)}).action_confirm()


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
        _register_papers(cls.env, cls.truck)
        cls.bravo_driver = Employee.create({'name': 'VC Bravo Driver', 'identification_id': '2400000003',
                                            'department_id': cls.dept_b.id})
        for employee in (cls.khalid, cls.bravo_driver):
            cls._licence(employee)

    @classmethod
    def _licence(cls, employee, licence_class='نقل ثقيل', expiry=None):
        doc = cls.env['ksw.document'].create({
            'type_id': cls.env.ref('KSW_documents.type_driving_licence').id, 'applies_to': 'employee',
            'employee_id': employee.id, 'number': f'L{employee.id}', 'license_class': licence_class,
            'expiry_date': expiry or date.today() + timedelta(days=365)})
        doc.action_confirm()
        return doc

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
        with self.assertRaisesRegex(UserError, 'not in VC Bravo'):  # the receiving department's own driver
            form.with_user(self.user_b).action_submit()
        form.with_user(self.user_b).write({'new_employee_id': self.bravo_driver.id})
        self.assertEqual(form.new_iqama, '2400000003')
        form.with_user(self.user_b).action_submit()
        self.assertEqual(form.state, 'hr')
        with self.assertRaises(UserError):  # the workshop cannot hand over before HR
            self._workshop_done(form)
        form.with_user(self.hr).action_hr_check()  # the licence comes from the register
        self.assertEqual((form.license_type, form.state), ('نقل ثقيل', 'workshop'))
        self._workshop_done(form)
        self.assertEqual(form.state, 'done')
        self.assertEqual(move.state, 'done')  # arrival: no separate confirmation
        self.assertEqual(self.truck.x_department_id, self.dept_b)
        self.assertEqual(self.truck.x_custodian_id, self.bravo_driver)
        self.assertEqual(self.truck.driver_id, self.bravo_driver)

    # Flow 3: driver change, the department's own: done at submit, HR told --
    def _driver_change(self, employee, user=None):
        return self.env['ksw.vehicle.custody.request'].with_user(user or self.user_a).create({
            'kind': 'driver_change', 'vehicle_id': self.truck.id, 'new_employee_id': employee.id,
            'change_reason': 'vacation'})

    def test_driver_change_route(self):
        Form = self.env['ksw.vehicle.custody.request']
        with self.assertRaises(UserError):  # hand-ins are not raised by hand
            Form.with_user(self.user_a).create({'kind': 'hand_in', 'vehicle_id': self.truck.id})
        with self.assertRaises(UserError):  # not his department's vehicle
            Form.with_user(self.user_b).create({'kind': 'driver_change', 'vehicle_id': self.truck.id})
        form = self._driver_change(self.khalid)
        self.assertEqual((form.previous_employee_id, form.department_id), (self.ahmed, self.dept_a))
        existing = form.message_ids.ids
        form.with_user(self.user_a).action_submit()
        # No workshop checkup, no HR step: done, and the vehicle has passed.
        self.assertEqual(form.state, 'done')
        self.assertFalse(form.workshop_request_id)
        self.assertEqual(self.truck.x_custodian_id, self.khalid)
        history = self.truck.x_custody_ids.sorted('id')
        self.assertEqual(history.mapped('employee_id'), self.ahmed | self.khalid)
        self.assertEqual(history[-1].source, form.name)
        self.assertEqual(self.truck.x_department_id, self.dept_a)
        # HR is told, with what the register lacks (this truck has no documents).
        told = form.message_ids.filtered(lambda m: m.id not in existing and self.hr.partner_id in m.partner_ids)
        self.assertTrue(told)
        self.assertIn('missing or expired in the register', told.body)

    def test_one_driver_one_vehicle(self):
        other = self.env['ksw.fleet.vehicle'].create({'name': 'VC-IS2', 'vehicle_type': 'isuzu',
                                                      'x_department_id': self.dept_a.id})
        other._ksw_set_custodian(self.khalid)
        form = self._driver_change(self.ahmed.browse())
        self.assertNotIn(self.khalid.id, form.allowed_new_employee_ids.ids)  # not even offered
        self.assertNotIn(self.ahmed.id, form.allowed_new_employee_ids.ids)  # the current custodian
        form.with_user(self.user_a).new_employee_id = self.khalid
        with self.assertRaisesRegex(UserError, 'already answers for VC-IS2'):
            form.with_user(self.user_a).action_submit()
        # The custody move itself refuses it, whatever route reached it.
        with self.assertRaisesRegex(UserError, 'as many vehicles as he may hold'):
            self.truck._ksw_set_custodian(self.khalid)
        # A Tedder is pulled by its head: it is nobody's second vehicle.
        tedder = self.env['ksw.fleet.vehicle'].create({'name': 'VC-TD1', 'vehicle_type': 'tank'})
        tedder._ksw_set_custodian(self.khalid)

    def test_vehicle_papers_before_it_changes_hands(self):
        """The operation card, insurance, istimara... of a company vehicle are
        in the register before it changes hands, or HR is asked for them."""
        bare = self.env['ksw.fleet.vehicle'].create({'name': 'VC-IS9', 'vehicle_type': 'isuzu',
                                                     'x_department_id': self.dept_a.id})
        bare.sudo().write({'client_id': self.env.company.partner_id.id, 'state': 'confirmed',
                           'x_registration_type': 'public'})
        bare._ksw_set_custodian(self.env['hr.employee'].create({'name': 'VC Bare', 'department_id': self.dept_a.id}))
        self.truck = bare
        needed = self.truck._ksw_handover_gaps()[self.truck.id]
        codes = {t.code for t, _s in needed}
        self.assertTrue({'istimara', 'insurance', 'mvpi', 'operation_card'} <= codes)
        self.assertNotIn('driver_card', codes)  # not checked until HR switches it on
        form = self._driver_change(self.khalid)
        with self.assertRaisesRegex(RedirectWarning, r'VC-IS9: Operation Card \(missing\)'):
            form.with_user(self.user_a).action_submit()
        form.with_user(self.user_a).action_request_documents()
        requests = self.env['ksw.document.request'].search([('vehicle_id', '=', self.truck.id)])
        self.assertEqual(set(requests.type_id.mapped('code')), codes)
        self.assertTrue(all(r.requested_by_id == self.user_a for r in requests))
        with self.assertRaisesRegex(UserError, 'HR has DR/'):
            form.with_user(self.user_a).action_submit()
        # HR registers them, from the requests: each closes, then it goes.
        for request in requests:
            doc = self.env['ksw.document'].browse(request.action_register()['res_id'])
            doc.write({'number': 'N-%s' % request.id, 'expiry_date': date.today() + timedelta(days=200)})
            doc.action_confirm()
        self.assertEqual(set(requests.mapped('state')), {'done'})
        form.with_user(self.user_a).action_submit()
        self.assertEqual(self.truck.x_custodian_id, self.khalid)

    def test_document_access_follows_the_departments(self):
        """Read only, same rule for vehicles and employees: a supervisor reads
        his departments' papers, the GM everybody's; neither changes anything."""
        bravo_truck = self.env['ksw.fleet.vehicle'].create({'name': 'VC-B1', 'vehicle_type': 'isuzu',
                                                            'x_department_id': self.dept_b.id})
        _register_papers(self.env, bravo_truck)
        Doc = self.env['ksw.document']
        alpha = Doc.search([('vehicle_id', '=', self.truck.id)])
        bravo = Doc.search([('vehicle_id', '=', bravo_truck.id)])
        licence = Doc.search([('employee_id', '=', self.khalid.id)])
        self.assertTrue(alpha and bravo and licence)
        # Same rule for employees: Khalid is in VC Alpha, so user_a reads his
        # licence; Bravo's driver's is not his to read.
        bravo_licence = Doc.search([('employee_id', '=', self.bravo_driver.id)])
        everything = alpha | bravo | licence | bravo_licence
        seen_a = Doc.with_user(self.user_a).search([('id', 'in', everything.ids)])
        self.assertEqual(seen_a, alpha | licence)
        seen_gm = Doc.with_user(self.gm).search([('id', 'in', everything.ids)])
        self.assertEqual(seen_gm, everything)
        self.assertFalse(alpha[:1].with_user(self.user_a).has_access('write'))
        self.assertFalse(alpha[:1].with_user(self.gm).has_access('write'))
        # The read-only list, read as the browser does, by a supervisor who is
        # no HR user (employee names come through the public profile).
        self.assertFalse(self.user_a.has_group('hr.group_hr_user'))
        Readonly = Doc.with_user(self.user_a)
        views = Readonly.get_views([(self.env.ref('KSW_vehicle_custody.view_ksw_document_list_readonly').id, 'list'),
                                    (self.env.ref('KSW_vehicle_custody.view_ksw_document_form_readonly').id, 'form')])
        spec = {f: {} for f in views['models']['ksw.document']['fields']
                if views['models']['ksw.document']['fields'][f]['type'] not in ('one2many', 'many2many')}
        rows = Readonly.web_search_read([('id', 'in', everything.ids)], spec)['records']
        self.assertEqual({r['id'] for r in rows}, set((alpha | licence).ids))
        Readonly.browse(licence.ids).web_read(spec)
        # The read-only list is offered to both, and lists what they may read.
        menu = self.env.ref('KSW_vehicle_custody.menu_vehicle_custody_documents')
        for user in (self.user_a, self.gm):
            self.assertIn(menu.id, self.env['ir.ui.menu'].with_user(user)._visible_menu_ids())

    def test_hr_allows_one_driver_two_vehicles(self):
        """An approved exception (the Azmi brothers each run two Isuzus)."""
        other = self.env['ksw.fleet.vehicle'].create({'name': 'VC-IS4', 'vehicle_type': 'isuzu',
                                                      'x_department_id': self.dept_a.id})
        other._ksw_set_custodian(self.khalid)
        with self.assertRaisesRegex(UserError, 'Say why'):
            self.khalid.write({'x_vehicle_limit': 2})
        self.khalid.write({'x_vehicle_limit': 2, 'x_vehicle_limit_reason': 'Runs two Isuzus on one route'})
        form = self._driver_change(self.khalid)
        self.assertIn(self.khalid.id, form.allowed_new_employee_ids.ids)
        form.with_user(self.user_a).action_submit()
        self.assertEqual(self.truck.x_custodian_id, self.khalid)
        # A third is still refused.
        third = self.env['ksw.fleet.vehicle'].create({'name': 'VC-IS5', 'vehicle_type': 'isuzu',
                                                      'x_department_id': self.dept_a.id})
        with self.assertRaisesRegex(UserError, r'may hold \(2\)'):
            third._ksw_set_custodian(self.khalid)

    def test_new_user_spoken_for_and_one_open_form(self):
        spare = self.env['ksw.fleet.vehicle'].create({'name': 'VC-IS3', 'vehicle_type': 'isuzu',
                                                      'x_department_id': self.dept_a.id})
        driver = self.env['hr.employee'].create({'name': 'VC Spare', 'department_id': self.dept_a.id})
        spare._ksw_set_custodian(driver)
        first = self._driver_change(self.khalid)
        # The supervisor reads his picker (no HR rights: the public model).
        self.assertIn(self.khalid.id, first.with_user(self.user_a).allowed_new_employee_ids.ids)
        picked = self.env['hr.employee'].with_user(self.user_a).name_search(
            'VC', domain=[('id', 'in', first.with_user(self.user_a).allowed_new_employee_ids.ids)])
        self.assertIn(self.khalid.id, [i for i, _n in picked])
        self.assertNotIn(driver.id, [i for i, _n in picked])  # holds VC-IS3
        with self.assertRaisesRegex(UserError, 'already has'), self.env.cr.savepoint():  # one open per vehicle
            self._driver_change(self.khalid)
        second = self.env['ksw.vehicle.custody.request'].with_user(self.user_a).create({
            'kind': 'driver_change', 'vehicle_id': spare.id, 'change_reason': 'vacation'})
        # A draft driver change is only an intention: both offer him, the first confirmed wins.
        self.assertIn(self.khalid.id, second.allowed_new_employee_ids.ids)
        second.with_user(self.user_a).new_employee_id = self.khalid
        first.with_user(self.user_a).action_submit()
        self.assertEqual(self.truck.x_custodian_id, self.khalid)
        with self.assertRaisesRegex(UserError, 'already answers for VC-IS1'):
            second.with_user(self.user_a).action_submit()
        second.with_user(self.user_a).action_cancel_draft()
        self.assertEqual(second.state, 'cancelled')

    def test_new_user_licence_from_the_register(self):
        newcomer = self.env['hr.employee'].create({'name': 'VC Newcomer', 'department_id': self.dept_a.id})
        form = self._driver_change(newcomer)
        with self.assertRaises(RedirectWarning) as caught:
            form.with_user(self.user_a).action_submit()
        self.assertEqual(caught.exception.args[1], 'KSW_vehicle_custody.action_request_new_user_licence')
        form.with_user(self.user_a).action_request_documents()
        dr = self.env['ksw.document.request'].search([('employee_id', '=', newcomer.id)])
        self.assertEqual((dr.state, dr.origin), ('requested', form.name))
        with self.assertRaisesRegex(UserError, 'HR has DR/'):
            form.with_user(self.user_a).action_submit()
        # A private licence does not drive a truck: HR cannot fix that by registering it.
        self._licence(newcomer, licence_class='خاصة')
        self.assertEqual(dr.state, 'done')
        with self.assertRaisesRegex(UserError, 'needs'):
            form.with_user(self.user_a).action_submit()
        self._licence(newcomer, licence_class='نقل خفيف', expiry=date.today() + timedelta(days=400))
        form.with_user(self.user_a).action_submit()
        self.assertEqual(self.truck.x_custodian_id, newcomer)

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

    def test_todos_skip_admin(self):
        """admin holds the workshop role for system access: no to-do for him."""
        admin = self.env.ref('base.user_admin')
        admin.sudo().group_ids = [(4, self.env.ref('KSW_workshop.group_workshop_manager').id),
                                  (4, self.env.ref('KSW_vehicle_custody.group_custody_hr').id)]
        request, form = self._release('holding')
        users = form.activity_ids.user_id
        self.assertIn(self.workshop, users)
        self.assertIn(self.hr, users)
        self.assertNotIn(admin, users)

    def test_workshop_staff_see_their_own_workshop_only(self):
        """The Tabuk manager handles Tabuk's forms; the Nabiya manager does not
        see, get, or complete them (as with workshop requests)."""
        Workshop = self.env['ksw.workshop']
        nabiya, other = Workshop._main(), Workshop.create({'name': 'VC Other Workshop'})
        self.dept_a.x_workshop_id = other
        other_mgr = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'vc_other_ws', 'login': 'vc_other_ws',
            'group_ids': [(6, 0, [self.env.ref('KSW_workshop.group_workshop_manager').id])],
            'x_workshop_ids': [(6, 0, other.ids)]})
        self.workshop.sudo().x_workshop_ids = [(6, 0, nabiya.ids)]
        request, form = self._release('holding')
        self.assertEqual(form.workshop_id, other)
        Form = self.env['ksw.vehicle.custody.request']
        pending = [('x_is_pending_my_action', '=', True)]
        self.assertIn(form, Form.with_user(other_mgr).search(pending))
        self.assertNotIn(form, Form.with_user(self.workshop).search(pending))
        self.assertFalse(Form.with_user(self.workshop).search([('id', '=', form.id)]))
        self.assertEqual(form.activity_ids.filtered(lambda a: a.user_id.has_group(
            'KSW_workshop.group_workshop_manager')).user_id, other_mgr)
        form.sudo().inspection_datetime = '2026-11-12 08:00:00'
        form.with_user(other_mgr).action_workshop_done()
        self.assertTrue(form.workshop_done)
