import logging
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

    @api.model
    def sync_from_bas(self):
        try:
            conn = self._bas_connect()
            cursor = conn.cursor(as_dict=True)
        except Exception as e:
            _logger.error('KSW BAS account sync: connection failed: %s', e)
            return

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
            return
        finally:
            conn.close()

        now = fields.Datetime.now()
        existing = {r.bas_code: r for r in self.search([])}

        for row in rows:
            code = (row['DCODE1'] or '').strip()
            if not code:
                continue
            if code.startswith('120501'):
                category = 'advance'
            elif code.startswith('2102') or code.startswith('2105'):
                category = 'loan'
            else:
                category = 'other'

            vals = {
                'bas_code': code,
                'name_ar': (row['DNAME'] or '').strip(),
                'name_en': (row['DNAME2'] or '').strip(),
                'acc_type': f"{row['DACC_TYPE'] or ''}/{row['DACC_TYPE2'] or ''}",
                'opening_balance': float(row['DOLDACC'] or 0),
                'current_debit': float(row['DCURRENT1'] or 0),
                'current_credit': float(row['DCURRENT2'] or 0),
                'category': category,
                'last_synced': now,
            }
            if code in existing:
                existing[code].write(vals)
            else:
                self.create(vals)

        # Remove accounts that no longer exist in BAS
        synced_codes = {(row['DCODE1'] or '').strip() for row in rows}
        stale = self.search([('bas_code', 'not in', list(synced_codes))])
        if stale:
            stale.unlink()

        _logger.info('KSW BAS: synced %d accounts', len(rows))

    @api.model
    def action_sync_all(self):
        _logger.info('KSW BAS: starting full sync')
        self.sync_from_bas()
        self.env['ksw.bas.invoice'].sync_from_bas()
        self.env['ksw.bas.payment'].sync_from_bas()
        _logger.info('KSW BAS: sync complete')
