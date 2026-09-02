import logging
from datetime import date, datetime
from time import monotonic

from odoo import models, api

_logger = logging.getLogger(__name__)

try:
    import pymssql
    _PYMSSQL_AVAILABLE = True
except ImportError:
    pymssql = None
    _PYMSSQL_AVAILABLE = False
    _logger.warning(
        "KSW_ext_sync: pymssql is not installed — BAS sync will be unavailable. "
        "Run: pip install pymssql"
    )

_SERVER = '192.168.1.82'
_PORT = '59090'
_USER = 'odoo_reader'
_PASSWORD = 'OdooRead@KSW2024!'
_DATABASE = 'bas9ss'


class BASConnector(models.AbstractModel):
    _name = 'ksw.bas.connector'
    _description = 'BAS SQL Server Connector'

    def _bas_connect(self):
        if not _PYMSSQL_AVAILABLE:
            raise ImportError("pymssql is not installed. Run: pip install pymssql")
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

    # ------------------------------------------------------------------
    # Batched upsert
    # ------------------------------------------------------------------
    # The BAS mirrors are filled by a cron, and a cron is NOT exempt from
    # ``limit_time_real``: ``server.py`` honours ``limit_time_real_cron``
    # only when it is *positive*, so the prod setting of ``-1`` falls
    # through to the same 120s (see KSW_payroll, pitfall #107).  On a
    # ``workers = 0`` server an overrun reloads the whole process.
    #
    # A row-at-a-time create/write loop over a fiscal year of invoices never
    # reached the end: killed at ~179s every run, rolled back in full, and
    # the four BAS tables sat at **0 rows** for weeks while production
    # restarted every four minutes.  Nothing was ever synced, and nothing in
    # the sync's own logging said so — it logs per step, and the steps it
    # logged were the ones that later rolled back.

    _BAS_CHUNK = 500

    @staticmethod
    def _bas_value_differs(current, incoming):
        """Compare a stored value with an incoming one, tolerantly."""
        if isinstance(current, float) or isinstance(incoming, float):
            return abs((current or 0.0) - (incoming or 0.0)) > 0.005
        # Odoo hands back date/datetime; BAS hands back datetime for both.
        if isinstance(current, date) and isinstance(incoming, datetime) \
                and not isinstance(current, datetime):
            incoming = incoming.date()
        return (current or False) != (incoming or False)

    def _bas_upsert(self, key_field, vals_list, compare_fields,
                    deadline=None, commit=True):
        """Create/update mirror rows in batches.

        Returns ``(created, updated, done)``.  ``done`` is False when
        ``deadline`` (a ``monotonic()`` timestamp) cut the pass short — the
        caller must then **not** advance its watermark, so the next run picks
        up the remainder.

        Rows whose ``compare_fields`` are unchanged are skipped entirely.
        That is what makes the steady state cheap: every run re-reads a
        30-day lookback window in which almost nothing has moved.
        ``last_synced`` is deliberately not a compare field — stamping it
        would make every row look changed and put us back where we started.
        """
        keys = [v[key_field] for v in vals_list if v.get(key_field)]
        existing = {}
        for i in range(0, len(keys), 1000):
            for rec in self.search([(key_field, 'in', keys[i:i + 1000])]):
                existing[rec[key_field]] = rec

        to_create, to_write = [], []
        for vals in vals_list:
            rec = existing.get(vals.get(key_field))
            if rec is None:
                to_create.append(vals)
            elif any(self._bas_value_differs(rec[f], vals.get(f))
                     for f in compare_fields):
                to_write.append((rec, vals))

        created = updated = 0
        for i in range(0, len(to_create), self._BAS_CHUNK):
            if deadline is not None and monotonic() > deadline:
                return created, updated, False
            chunk = to_create[i:i + self._BAS_CHUNK]
            self.create(chunk)
            created += len(chunk)
            if commit:
                self.env.cr.commit()

        for i in range(0, len(to_write), self._BAS_CHUNK):
            if deadline is not None and monotonic() > deadline:
                return created, updated, False
            for rec, vals in to_write[i:i + self._BAS_CHUNK]:
                rec.write(vals)
                updated += 1
            if commit:
                self.env.cr.commit()

        return created, updated, True

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

