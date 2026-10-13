from odoo import http
from odoo.exceptions import AccessDenied
from odoo.tools import config

from odoo.addons.web.controllers.database import Database


class KswDatabase(Database):

    @http.route('/web/database/list', type='jsonrpc', auth='none')
    def list(self):
        # Only bypass list_db when the server is pinned to its own database(s);
        # an unfiltered server would otherwise reveal every database name.
        if not (config['dbfilter'] or config['db_name']):
            raise AccessDenied()
        return http.db_list(force=True)
