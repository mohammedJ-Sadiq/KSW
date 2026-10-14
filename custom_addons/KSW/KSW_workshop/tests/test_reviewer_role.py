import importlib.util
import os

from lxml import etree

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestReviewerRole(TransactionCase):
    """A Reviewer reads every request and the work done on it, and changes
    nothing; only real workshop managers are told about new requests."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)

        def _user(login, groups):
            user = Users.create({
                'name': login, 'login': login, 'email': f'{login}@rev.test',
                'group_ids': [(6, 0, [cls.env.ref(g).id for g in groups])]})
            cls.env['hr.employee'].create({'name': login, 'user_id': user.id})
            return user

        cls.reviewer = _user('rev_reviewer', ('KSW_workshop.group_workshop_reviewer',))
        cls.manager = _user('rev_manager', ('KSW_workshop.group_workshop_manager',))
        cls.requester = _user('rev_requester', ('KSW_workshop.group_workshop_requester',))
        cls.vehicle = cls.env['ksw.fleet.vehicle'].create({'name': 'REV1', 'vehicle_type': 'isuzu'})
        cls.request = cls.env['ksw.workshop.request'].with_user(cls.requester).create({
            'vehicle_type': 'isuzu', 'vehicle_id': cls.vehicle.id,
            'driver_id': cls.requester.employee_id.id, 'description': 'Oil change'})
        cls.request.with_user(cls.manager).action_start()

    def test_reviewer_reads_any_request_and_its_work(self):
        request = self.request.with_user(self.reviewer)
        self.assertIn(request, request.search([]))
        request.read(['name', 'state', 'note', 'labor_line_ids', 'part_line_ids'])

    def test_reviewer_cannot_change_anything(self):
        request = self.request.with_user(self.reviewer)
        with self.assertRaises(UserError):
            request.action_complete()
        with self.assertRaises(UserError):
            request.write({'note': 'looks fine'})
        with self.assertRaises(UserError):
            request.write({'odometer_reading': 1000})
        with self.assertRaises(UserError):
            request.write({'description': 'changed'})
        with self.assertRaises(AccessError):
            request.unlink()

    def test_reviewer_views_are_read_only(self):
        Request = self.env['ksw.workshop.request']
        for view_type in ('form', 'list'):
            arch = etree.fromstring(Request.with_user(self.reviewer).get_view(view_type=view_type)['arch'])
            self.assertEqual(arch.get('edit'), '0', view_type)
            self.assertEqual(arch.get('create'), '0', view_type)
            arch = etree.fromstring(Request.with_user(self.manager).get_view(view_type=view_type)['arch'])
            self.assertNotEqual(arch.get('edit'), '0', view_type)

    def test_reviewer_sees_the_repair_report_page(self):
        arch = self.env['ksw.workshop.request'].with_user(self.reviewer).get_view(view_type='form')['arch']
        self.assertIn('odometer_reading', arch)
        self.assertIn('name="note"', arch)

    def test_reviewer_is_not_notified_and_admin_is_not_emailed(self):
        admin = self.env.ref('base.user_admin')
        admin.write({'group_ids': [(4, self.env.ref('KSW_workshop.group_workshop_manager').id)]})
        request = self.env['ksw.workshop.request'].with_user(self.requester).create({
            'vehicle_type': 'isuzu', 'vehicle_id': self.vehicle.id,
            'driver_id': self.requester.employee_id.id, 'description': 'Brakes'})
        message = request.message_ids.filtered(lambda m: 'New workshop request' in (m.body or ''))
        self.assertIn(self.manager.partner_id, message.partner_ids)
        self.assertNotIn(admin.partner_id, message.partner_ids)
        self.assertNotIn(self.reviewer.partner_id, message.partner_ids)


@tagged('post_install', '-at_install')
class TestManagersBecomeReviewers(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        path = os.path.join(os.path.dirname(__file__), '..', 'migrations', '19.0.12.4.0', 'post-migrate.py')
        spec = importlib.util.spec_from_file_location('ksw_workshop_12_4_0', path)
        cls.migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.migration)
        cls.manager_group = cls.env.ref('KSW_workshop.group_workshop_manager')
        cls.reviewer_group = cls.env.ref('KSW_workshop.group_workshop_reviewer')
        # Start from a known manager list, whatever this database holds.
        cls.manager_group.user_ids.write({'group_ids': [(3, cls.manager_group.id)]})
        Users = cls.env['res.users'].with_context(no_reset_password=True)

        def _manager(login, name):
            return Users.create({'name': name, 'login': login,
                                 'group_ids': [(6, 0, [cls.manager_group.id])]})
        cls.other = _manager('mig_other', 'Some Manager')
        cls.admin = cls.env.ref('base.user_admin')
        cls.admin.write({'group_ids': [(4, cls.manager_group.id)]})

    def test_everyone_but_essam_and_admin_becomes_reviewer(self):
        essam = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Essam A. JABER', 'login': 'mig_essam',
            'group_ids': [(6, 0, [self.manager_group.id])]})
        self.migration.migrate(self.env.cr, '19.0.12.3.0')
        self.assertIn(self.manager_group, essam.group_ids)
        self.assertIn(self.manager_group, self.admin.group_ids)
        self.assertNotIn(self.manager_group, self.other.group_ids)
        self.assertIn(self.reviewer_group, self.other.group_ids)

    def test_no_essam_means_nothing_moves(self):
        self.migration.migrate(self.env.cr, '19.0.12.3.0')
        self.assertIn(self.manager_group, self.other.group_ids)
        self.assertNotIn(self.reviewer_group, self.other.group_ids)


@tagged('post_install', '-at_install')
class TestReviewerMenu(TransactionCase):

    def _menu_ids(self, groups):
        user = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': f'menu_{"_".join(g.split(".")[-1] for g in groups)}',
            'login': f'menu_{"_".join(g.split(".")[-1] for g in groups)}',
            'group_ids': [(6, 0, [self.env.ref(g).id for g in ('base.group_user',) + groups])]})
        menus = self.env['ir.ui.menu'].with_user(user).load_menus(False)
        return {m['id'] for m in menus.values() if isinstance(m, dict) and 'id' in m}

    def test_reviewer_has_no_my_requests_but_all_requests(self):
        my = self.env.ref('KSW_workshop.menu_ksw_workshop_my').id
        all_ = self.env.ref('KSW_workshop.menu_ksw_workshop_all').id
        reviewer = self._menu_ids(('KSW_workshop.group_workshop_reviewer',))
        self.assertNotIn(my, reviewer)
        self.assertIn(all_, reviewer)

    def test_everyone_else_keeps_my_requests(self):
        my = self.env.ref('KSW_workshop.menu_ksw_workshop_my').id
        self.assertIn(my, self._menu_ids(()))
        self.assertIn(my, self._menu_ids(('KSW_workshop.group_workshop_requester',)))
        self.assertIn(my, self._menu_ids(('KSW_workshop.group_workshop_reviewer',
                                          'KSW_workshop.group_workshop_requester')))
