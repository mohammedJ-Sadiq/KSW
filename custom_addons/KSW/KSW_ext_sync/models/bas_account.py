import logging
from time import monotonic

from odoo import models, fields, api

_logger = logging.getLogger(__name__)

# COD10 account code prefixes we care about
_ACCOUNT_PREFIXES = (
    '120501',   # Employee advances (سلف العاملين)
    '2102',     # Short-term bank loans (قروض قصيرة الاجل)
    '2105',     # Other loans / financing
)


class BASAccount(models.Model):
    _name = 'ksw.bas.account'
    _description = 'BAS GL Account (Loans & Advances)'
    _inherit = ['ksw.bas.connector']
    _order = 'bas_code'

    bas_code = fields.Char('Account Code', readonly=True, index=True)
    name_ar = fields.Char('Name (Arabic)', readonly=True)
    name_en = fields.Char('Name (English)', readonly=True)
    acc_type = fields.Char('Type', readonly=True)
    opening_balance = fields.Float('Opening Balance', readonly=True, digits=(16, 2))
    current_debit = fields.Float('Current Debit', readonly=True, digits=(16, 2))
    current_credit = fields.Float('Current Credit', readonly=True, digits=(16, 2))
    balance = fields.Float(
        'Balance', compute='_compute_balance', store=True, digits=(16, 2),
    )
    category = fields.Selection([
        ('advance', 'Employee Advance'),
        ('loan', 'Bank Loan'),
        ('other', 'Other'),
    ], string='Category', readonly=True)
    last_synced = fields.Datetime('Last Synced', readonly=True)

    @api.depends('opening_balance', 'current_debit', 'current_credit')
    def _compute_balance(self):
        for rec in self:
            debit = rec.current_debit or 0.0
            credit = rec.current_credit or 0.0
            rec.balance = rec.opening_balance + debit - credit

    # Everything but ``last_synced`` — see ``_bas_upsert``.
    _COMPARE_FIELDS = (
        'name_ar', 'name_en', 'acc_type', 'opening_balance',
        'current_debit', 'current_credit', 'category',
    )

    @api.model
    def sync_from_bas(self, deadline=None, commit=True):
        try:
            conn = self._bas_connect()
            cursor = conn.cursor(as_dict=True)
        except Exception as e:
            _logger.error('KSW BAS account sync: connection failed: %s', e)
            return False

        placeholders = ','.join(['%s'] * len(_ACCOUNT_PREFIXES))
        conditions = ' OR '.join(f"DCODE1 LIKE '{p}%'" for p in _ACCOUNT_PREFIXES)

        try:
            cursor.execute(f"""
                SELECT DCODE1, DNAME, DNAME2, DACC_TYPE, DACC_TYPE2,
                       ISNULL(DOLDACC, 0) AS DOLDACC,
                       ISNULL(DCURRENT1, 0) AS DCURRENT1,
                       ISNULL(DCURRENT2, 0) AS DCURRENT2
                FROM COD10
                WHERE {conditions}
                ORDER BY DCODE1
            """)
            rows = cursor.fetchall()
        except Exception as e:
            _logger.error('KSW BAS account sync: query failed: %s', e)
            conn.close()
            return False
        finally:
            conn.close()

        now = fields.Datetime.now()

        def _category(code):
            if code.startswith('120501'):
                return 'advance'
            if code.startswith('2102') or code.startswith('2105'):
                return 'loan'
            return 'other'

        vals_list = []
        for row in rows:
            code = (row['DCODE1'] or '').strip()
            if not code:
                continue
            vals_list.append({
                'bas_code': code,
                'name_ar': (row['DNAME'] or '').strip(),
                'name_en': (row['DNAME2'] or '').strip(),
                'acc_type': f"{row['DACC_TYPE'] or ''}/{row['DACC_TYPE2'] or ''}",
                'opening_balance': float(row['DOLDACC'] or 0),
                'current_debit': float(row['DCURRENT1'] or 0),
                'current_credit': float(row['DCURRENT2'] or 0),
                'category': _category(code),
                'last_synced': now,
            })

        created, updated, done = self._bas_upsert(
            'bas_code', vals_list, self._COMPARE_FIELDS,
            deadline=deadline, commit=commit)

        if done:
            # Only prune on a complete pass — a partial read would delete
            # every account the budget never got to.
            synced_codes = [v['bas_code'] for v in vals_list]
            stale = self.search([('bas_code', 'not in', synced_codes)])
            if stale:
                stale.unlink()

        _logger.info(
            'KSW BAS: accounts %s — %d read, %d created, %d updated',
            'complete' if done else 'INCOMPLETE (time budget)',
            len(rows), created, updated)
        return done

    # Well under `limit_time_real` (120s in prod).  A cron thread is NOT
    # exempt from it — `limit_time_real_cron = -1` falls through to
    # `limit_time_real`, and on `workers = 0` an overrun reloads the whole
    # server.  See `ksw.bas.connector._bas_upsert`.
    #
    # The budget is checked *between* chunks and *before* each step, so the
    # worst overshoot is one step's pre-work: querying BAS and building the
    # vals list for a whole fiscal year measured at **27s** (284k invoices +
    # 22k payments, Sep 2026).  A step starting at 69.9s therefore lands
    # near 97s — still inside 120s, with the chunk loop stopping at once.
    _SYNC_ALL_SECONDS = 70

    @api.model
    def action_sync_all(self, commit=True):
        """Run every BAS mirror, newest-cheapest first, inside one budget.

        Each step commits its own chunks, so a run that runs out of budget
        keeps everything it managed to write and the next run continues from
        there — instead of the previous behaviour, where the invoice step
        overran, the cron was killed, and **the entire transaction rolled
        back**.  That is why all four tables sat at 0 rows.
        """
        _logger.info('KSW BAS: starting full sync')
        deadline = monotonic() + self._SYNC_ALL_SECONDS
        steps = (
            ('accounts', self),
            ('customers', self.env['ksw.bas.customer']),
            ('invoices', self.env['ksw.bas.invoice']),
            ('payments', self.env['ksw.bas.payment']),
        )
        incomplete = []
        for label, model in steps:
            if monotonic() > deadline:
                incomplete.append(label)
                continue
            if model.sync_from_bas(deadline=deadline, commit=commit) is False:
                incomplete.append(label)
        if incomplete:
            _logger.info(
                'KSW BAS: sync incomplete (%s) — continues on the next run.',
                ', '.join(incomplete))
        else:
            _logger.info('KSW BAS: sync complete')
        return not incomplete
