import logging
import pymssql
from odoo import models, api

_logger = logging.getLogger(__name__)

_SERVER = '192.168.1.82'
_PORT = '59090'
_USER = 'odoo_reader'
_PASSWORD = 'OdooRead@KSW2024!'
_DATABASE = 'bas9ss'


class BASConnector(models.AbstractModel):
    _name = 'ksw.bas.connector'
    _description = 'BAS SQL Server Connector'

    def _bas_connect(self):
        p = self.env['ir.config_parameter'].sudo()
        return pymssql.connect(
            server=p.get_param('ksw_bas.server', _SERVER),
            port=p.get_param('ksw_bas.port', _PORT),
            user=p.get_param('ksw_bas.user', _USER),
            password=p.get_param('ksw_bas.password', _PASSWORD),
            database=p.get_param('ksw_bas.database', _DATABASE),
            login_timeout=15,
            charset='UTF-8',
        )

    def action_test_connection(self):
        try:
            conn = self._bas_connect()
            cursor = conn.cursor()
            cursor.execute('SELECT @@VERSION')
            version = cursor.fetchone()[0]
            conn.close()
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'BAS Connection OK',
                    'message': version[:80],
                    'type': 'success',
                },
            }
        except Exception as e:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'BAS Connection Failed',
                    'message': str(e),
                    'type': 'danger',
                },
            }

