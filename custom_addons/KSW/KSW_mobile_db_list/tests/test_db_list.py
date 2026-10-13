import json
from unittest.mock import patch

from odoo.exceptions import AccessDenied
from odoo.service import db as db_service
from odoo.tests import HttpCase, tagged
from odoo.tools import config


@tagged('post_install', '-at_install')
class TestMobileDbList(HttpCase):

    def _post(self, path):
        return self.url_open(
            path,
            data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': {}}),
            headers={'Content-Type': 'application/json'},
        )

    def test_list_returns_only_current_db(self):
        res = self._post('/web/database/list').json()
        self.assertNotIn('error', res)
        self.assertEqual(res['result'], [self.env.cr.dbname])

    def test_database_manager_stays_disabled(self):
        self.startPatcher(patch.dict(config.options, {'list_db': False}))
        res = self.url_open('/web/database/manager')
        self.assertIn('database manager has been disabled', res.text)
        # backup / drop / create / restore all sit behind this same guard
        guarded = db_service.check_db_management_enabled(lambda: 'reached')
        with self.assertRaises(AccessDenied):
            guarded()

    def test_unfiltered_server_still_refuses(self):
        self.startPatcher(patch.dict(config.options, {'dbfilter': '', 'db_name': ''}))
        res = self._post('/web/database/list').json()
        self.assertEqual(res['error']['data']['name'], 'odoo.exceptions.AccessDenied')
