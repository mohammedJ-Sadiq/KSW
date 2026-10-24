from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged

PERIODS = ('month', 'last_month', 'quarter', 'year')


@tagged('post_install', '-at_install')
class TestWorkshopDashboard(TransactionCase):
    """The overview is the Reviewer's, and every figure opens exactly the requests it counts."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)

        def _user(login, groups):
            user = Users.create({'name': login, 'login': login, 'email': f'{login}@dash.test',
                                 'group_ids': [(6, 0, [cls.env.ref(g).id for g in groups])]})
            cls.env['hr.employee'].create({'name': login, 'user_id': user.id})
            return user

        cls.reviewer = _user('dash_reviewer', ('KSW_workshop.group_workshop_reviewer',))
        cls.manager = _user('dash_manager', ('KSW_workshop.group_workshop_manager',))
        cls.technician = _user('dash_technician', ('KSW_workshop.group_workshop_technician',))
        cls.requester = _user('dash_requester', ('KSW_workshop.group_workshop_requester',))
        cls.vehicle = cls.env['ksw.fleet.vehicle'].create({'name': 'DASH1', 'vehicle_type': 'isuzu',
                                                         'driver_id': cls.requester.employee_id.id})
        Request = cls.env['ksw.workshop.request'].with_user(cls.requester)
        cls.first, cls.second, cls.tyres, cls.waiting = [Request.create({
            'vehicle_type': 'isuzu', 'vehicle_id': cls.vehicle.id,
            'driver_id': cls.requester.employee_id.id, 'description': desc})
            for desc in ('Brakes', 'Brakes again', 'Tyres', 'Oil change')]
        # Back-date the history directly: the write guard (rightly) refuses raw
        # state and date writes, and create_date is not writable through the ORM.
        now = fields.Datetime.now()
        brakes = cls.env.ref('KSW_workshop.request_type_brakes').id
        tyres = cls.env.ref('KSW_workshop.request_type_tyres').id
        for request, created, completed, rtype in (
                (cls.first, now - timedelta(days=8), now - timedelta(days=5), brakes),
                (cls.second, now - timedelta(days=1), now, brakes),
                (cls.tyres, now - timedelta(days=1), now, tyres),
                (cls.waiting, now - timedelta(days=4), None, brakes)):
            cls.env.cr.execute(
                "UPDATE ksw_workshop_request SET create_date = %s, completion_date = %s, state = %s WHERE id = %s",
                (created, completed, 'completed' if completed else 'new', request.id))
            cls.env.cr.execute("DELETE FROM ksw_workshop_request_type_rel WHERE request_id = %s", (request.id,))
            cls.env.cr.execute("INSERT INTO ksw_workshop_request_type_rel (request_id, type_id) VALUES (%s, %s)",
                               (request.id, rtype))
        cls.env['ksw.workshop.request'].invalidate_model()
        cls.jalal = cls.env['ksw.workshop.technician'].create({'name': 'DASH Jalal'})
        cls.env['ksw.workshop.labor.line'].sudo().create([
            {'request_id': cls.second.id, 'description': 'Pads', 'technician_id': cls.jalal.id},
            {'request_id': cls.second.id, 'description': 'Discs', 'technician_id': cls.jalal.id},
            {'request_id': cls.tyres.id, 'description': 'Tyres', 'technician_id': cls.jalal.id}])

    def _data(self, user, period='month'):
        return self.env['ksw.workshop.dashboard'].with_user(user).get_workshop_data(period)

    def test_only_reviewers_and_managers(self):
        self._data(self.reviewer)
        self._data(self.manager)
        for user in (self.technician, self.requester):
            with self.assertRaises(AccessError):
                self._data(user)
        menu = self.env.ref('KSW_workshop_dashboard.menu_ksw_workshop_overview')
        Menu = self.env['ir.ui.menu']
        self.assertIn(menu.id, Menu.with_user(self.reviewer)._visible_menu_ids())
        self.assertIn(menu.id, Menu.with_user(self.manager)._visible_menu_ids())
        self.assertNotIn(menu.id, Menu.with_user(self.technician)._visible_menu_ids())
        self.assertNotIn(menu.id, Menu.with_user(self.requester)._visible_menu_ids())

    def test_every_figure_opens_what_it_counts(self):
        Request = self.env['ksw.workshop.request'].with_user(self.reviewer)
        for period in PERIODS:
            d = self._data(self.reviewer, period)
            for key in ('open', 'new', 'in_progress', 'received', 'completed', 'repeat'):
                self.assertEqual(Request.search_count(d[key]['domain']), d[key]['count'], (period, key))
            self.assertEqual(Request.search_count(d['new']['stale_domain']), d['new']['stale'])
            self.assertEqual(sum(b['count'] for b in d['age']), d['open']['count'], period)
            self.assertEqual(sum(s['count'] for s in d['by_state']), d['open']['count'], period)
            for rows in ('vehicles', 'types', 'clients'):
                for row in d[rows]:
                    self.assertEqual(Request.search_count(row['domain']), row['count'], (period, rows))
            for row in d['technicians']:
                self.assertEqual(Request.search_count(row['domain']), row['completed'] + row['open'])
            # The chart covers the period on the page.
            self.assertEqual(sum(t['received'] for t in d['trend']), d['received']['count'], period)
            self.assertEqual(sum(t['completed'] for t in d['trend']), d['completed']['count'], period)

    def test_repeat_repair(self):
        d = self._data(self.reviewer, 'year')
        repeats = self.env['ksw.workshop.request'].search(d['repeat']['domain'])
        # Back for brakes four days after the first brake job was completed: a
        # repeat. Tyres on the same vehicle is a different job, the first job had
        # nothing before it, and the open one is not completed.
        self.assertIn(self.second, repeats)
        self.assertNotIn(self.tyres, repeats)
        self.assertNotIn(self.first, repeats)
        self.assertNotIn(self.waiting, repeats)
        for row in d['repeat']['rows']:
            self.assertEqual(len(repeats & self.env['ksw.workshop.request'].search(row['domain'])), row['count'])

    def test_technicians_come_from_the_labour_lines(self):
        # The request's own technician_id is left empty in practice; who did the
        # job is on its labour lines. Two lines on one request is one job.
        row = next(r for r in self._data(self.reviewer)['technicians'] if r['name'] == 'DASH Jalal')
        self.assertEqual((row['completed'], row['open']), (2, 0))
        self.assertEqual(self.env['ksw.workshop.request'].search(row['domain']), self.second | self.tyres)

    def test_backlog_age(self):
        d = self._data(self.reviewer)
        bucket = next(b for b in d['age'] if b['key'] == '3_7')
        self.assertIn(self.waiting, self.env['ksw.workshop.request'].search(bucket['domain']))
        self.assertIn(self.waiting, self.env['ksw.workshop.request'].search(d['new']['stale_domain']))

    def test_listed_in_the_dashboards_app_for_reviewers_only(self):
        entry = self.env.ref('KSW_workshop_dashboard.hub_dashboard_workshop')
        self.assertEqual(entry.x_client_action_tag, 'ksw_workshop_dashboard')
        Dashboard = self.env['spreadsheet.dashboard']
        self.assertIn(entry, Dashboard.with_user(self.reviewer).search([]))
        self.assertIn(entry, Dashboard.with_user(self.manager).search([]))
        self.assertNotIn(entry, Dashboard.with_user(self.technician).search([]))
        self.assertNotIn(entry, Dashboard.with_user(self.requester).search([]))

