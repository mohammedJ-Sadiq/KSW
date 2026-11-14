import json

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged
from odoo.tools.safe_eval import safe_eval


@tagged('post_install', '-at_install')
class TestHomeCounters(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Source = cls.env['ksw.home.inbox.source']
        cls.Source.search([]).write({'active': False})  # only this test's counters
        cls.user = new_test_user(cls.env, login='ksw_home_user', groups='base.group_user')
        cls.partner = cls.env['res.partner'].create({'name': 'KSW Home Probe'})

    def _counters(self, user=None):
        return self.Source.with_user(user or self.user).search([])._counters()

    def test_counts_under_users_access_and_hides_zero(self):
        self.Source.create({'name': 'Probe', 'model_name': 'res.partner',
                            'domain': "[('name', '=', 'KSW Home Probe')]"})
        self.Source.create({'name': 'Empty', 'model_name': 'res.partner',
                            'domain': "[('name', '=', 'no such partner at all')]"})
        counters = self._counters()
        self.assertEqual([c['name'] for c in counters], ['Probe'])
        self.assertEqual(counters[0]['count'], 1)
        self.assertEqual(counters[0]['action']['res_model'], 'res.partner')

    def test_counter_names_the_app_its_records_live_in(self):
        """Opening a counter must put its own app in the top bar, not the last one used."""
        admin = self.env.ref('base.user_admin')
        app_id = self.Source.with_user(admin)._app_for_model('res.partner')
        self.assertTrue(app_id)
        self.assertFalse(self.env['ir.ui.menu'].browse(app_id).parent_id, 'must be an app (root menu)')
        self.assertFalse(self.Source._app_for_model('no.such.model'))
        self.Source.create({'name': 'Probe', 'model_name': 'res.partner',
                            'domain': "[('id', '=', %d)]" % self.partner.id})
        self.assertEqual(self._counters(admin)[0]['app_id'], app_id)

    def test_uninstalled_model_is_skipped(self):
        self.Source.create({'name': 'Ghost', 'model_name': 'no.such.model', 'domain': '[]'})
        self.assertEqual(self._counters(), [])

    def test_unreadable_model_is_skipped(self):
        # A plain internal user cannot read ir.config_parameter.
        self.Source.create({'name': 'Secret', 'model_name': 'ir.config_parameter', 'domain': '[]'})
        self.assertEqual(self._counters(), [])

    def test_group_restriction(self):
        self.Source.create({'name': 'Admins only', 'model_name': 'res.partner',
                            'domain': "[('id', '=', %d)]" % self.partner.id,
                            'group_ids': [(6, 0, self.env.ref('base.group_system').ids)]})
        self.assertEqual(self._counters(), [])
        self.assertEqual(len(self._counters(self.env.ref('base.user_admin'))), 1)

    def test_domain_sees_uid(self):
        self.Source.create({'name': 'Me', 'model_name': 'res.users', 'domain': "[('id', '=', uid)]"})
        self.assertEqual(self._counters()[0]['count'], 1)

    def test_invalid_domain_refused(self):
        with self.assertRaises(ValidationError):
            self.Source.create({'name': 'Bad', 'model_name': 'res.partner', 'domain': '[(1, '})

    def test_every_seeded_counter_runs_on_this_database(self):
        """A seeded counter that errors would silently vanish from Home."""
        res_ids = self.env['ir.model.data'].search(
            [('module', '=', 'KSW_backend_theme'), ('model', '=', 'ksw.home.inbox.source')]).mapped('res_id')
        seeded = self.Source.with_context(active_test=False).browse(res_ids)
        # 6 original + 4 for vehicles and drivers (Nov 2026).
        self.assertEqual(len(seeded), 10)
        admin = self.env.ref('base.user_admin')
        for source in seeded:
            Model = self.env.get(source.model_name)
            if Model is None:
                continue
            domain = safe_eval(source.domain, source.with_user(admin)._eval_context())
            Model.with_user(admin).search_count(domain)  # must not raise


@tagged('post_install', '-at_install')
class TestHomeLayout(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = new_test_user(cls.env, login='ksw_home_layout', groups='base.group_user')
        cls.admin = cls.env.ref('base.user_admin')
        cls.Users = cls.env['res.users']

    def test_home_data_shape(self):
        data = self.Users.with_user(self.user).ksw_home_data()
        self.assertEqual(set(data), {'user_name', 'counters', 'activities', 'config',
                                     'is_personal', 'can_set_default'})
        self.assertFalse(data['can_set_default'])
        self.assertEqual(set(data['activities']), {'overdue', 'today', 'planned'})

    def test_personal_layout_saved_cleaned_and_reset(self):
        Users = self.Users.with_user(self.user)
        Users.ksw_home_save_config({'order': ['a.b', None, 'c.d'], 'hidden': ['c.d'], 'junk': 1})
        data = Users.ksw_home_data()
        self.assertTrue(data['is_personal'])
        self.assertEqual(data['config'], {'order': ['a.b', 'c.d'], 'hidden': ['c.d']})
        Users.ksw_home_reset_config()
        self.assertFalse(Users.ksw_home_data()['is_personal'])

    def test_default_layout_admin_only(self):
        with self.assertRaises(AccessError):
            self.Users.with_user(self.user).ksw_home_save_default({'order': [], 'hidden': ['x.y']})
        self.Users.with_user(self.admin).ksw_home_save_default({'order': [], 'hidden': ['x.y']})
        data = self.Users.with_user(self.user).ksw_home_data()
        self.assertEqual(data['config']['hidden'], ['x.y'])
        self.assertFalse(data['is_personal'])

    def test_personal_layout_wins_over_default(self):
        self.Users.with_user(self.admin).ksw_home_save_default({'order': [], 'hidden': ['x.y']})
        self.Users.with_user(self.user).ksw_home_save_config({'order': [], 'hidden': []})
        self.assertEqual(self.Users.with_user(self.user).ksw_home_data()['config']['hidden'], [])

    def test_broken_stored_layout_falls_back(self):
        self.user.ksw_home_config = 'not json'
        data = self.Users.with_user(self.user).ksw_home_data()
        self.assertFalse(data['is_personal'])
        self.assertEqual(json.loads(json.dumps(data['config'])), {'order': [], 'hidden': []})


@tagged('post_install', '-at_install')
class TestHomeHttp(HttpCase):

    def test_home_route_and_rpc(self):
        new_test_user(self.env, login='ksw_home_http', password='ksw_home_http_pw1', groups='base.group_user')
        self.authenticate('ksw_home_http', 'ksw_home_http_pw1')
        self.assertEqual(self.url_open('/odoo/home').status_code, 200)
        data = self.make_jsonrpc_request('/web/dataset/call_kw/res.users/ksw_home_data', {
            'model': 'res.users', 'method': 'ksw_home_data', 'args': [], 'kwargs': {},
        })
        self.assertIn('counters', data)
