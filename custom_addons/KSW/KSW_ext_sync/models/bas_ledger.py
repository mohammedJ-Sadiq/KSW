"""Customer-ledger lines mirrored from BAS (VOU10).

Every journal line that debits or credits a customer account (``1203*``)
with a non-zero amount: month-end sales invoices (FTYPE 002), cash and bank
receipts (018 / 015) and anything else that moves a customer balance.

Why this exists next to ``ksw.bas.invoice`` / ``ksw.bas.payment``: credit
customers are billed by ONE month-end invoice per customer (FTYPE 002), and
the daily delivery tickets (FTYPE 600) post zero to the customer. The
invoice mirror reads INV10 001/600, so it never saw a credit customer's
real sales. Reconciled 2026-10-21 against the accountant's March 2026
commission workbook: the 002 debits matched 408 of 408 customers to the
riyal.

Each line also carries ``SELLER`` / ``SELLERNAME`` — the rep BAS stamped on
that document when it was posted. That is the historical truth the
commission workbook groups by; COD10's SELLER / SELLER2 is only the rep
assigned *today*.

Lines are keyed on VOU10's real primary key (FTYPE+FTYPE2+CODE2+NUMBER1+
SERIAL). The re-scanned window is authoritative: a line BAS no longer has
in that window is deleted here, so an edited or cancelled voucher does not
linger in the mirror.
"""
import logging
from datetime import datetime, timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# BAS posts month-end invoices days after their FDATE (seen: 9 days); keep
# re-reading a generous trailing window. Same reasoning as bas_payment.py.
_LOOKBACK_DAYS = 45
_ACCOUNT_LIKE = '1203%'


class BASLedgerLine(models.Model):
    _name = 'ksw.bas.ledger.line'
    _description = 'BAS Customer Ledger Line'
    _inherit = ['ksw.bas.connector']
    _order = 'date desc, id desc'

    bas_key = fields.Char('BAS Key', readonly=True, index=True)
    ftype = fields.Char('Doc Type', readonly=True, index=True,
                        help='VOU10.FTYPE: 002 month-end sales invoice, '
                             '018 cash receipt, 015 bank receipt, ...')
    ftype2 = fields.Integer('Sub-type', readonly=True)
    branch_code = fields.Char('Branch', readonly=True)
    number = fields.Char('Doc No.', readonly=True)
    date = fields.Date('Date', readonly=True, index=True)
    debit_account = fields.Char('Debit Account', readonly=True, index=True)
    credit_account = fields.Char('Credit Account', readonly=True, index=True)
    amount = fields.Float('Amount', readonly=True, digits=(16, 2),
                          help='As posted in BAS, VAT included.')
    tax_amount = fields.Float('VAT (BAS)', readonly=True, digits=(16, 2))
    seller_code = fields.Char('Rep Code', readonly=True, index=True,
                              help='VOU10.SELLER on this document.')
    seller_name = fields.Char('Rep Name (BAS)', readonly=True)
    term_days = fields.Integer('Term (Days)', readonly=True,
                               help='VOU10.DUTY_DAY on this document.')
    description = fields.Char('Description', readonly=True)
    last_synced = fields.Datetime('Last Synced', readonly=True)

    _COMPARE_FIELDS = (
        'ftype', 'ftype2', 'branch_code', 'number', 'date', 'debit_account',
        'credit_account', 'amount', 'tax_amount', 'seller_code',
        'seller_name', 'term_days', 'description',
    )

    @staticmethod
    def _make_key(ftype, ftype2, code2, number1, serial):
        return (f'{ftype}_{ftype2}_{(code2 or "").strip()}_'
                f'{int(number1 or 0)}_{int(serial or 0)}')

    @staticmethod
    def _code(value):
        if value in (None, '', -1, '-1'):
            return ''
        try:
            return str(int(value))
        except (TypeError, ValueError):
            return str(value).strip()

    @api.model
    def sync_from_bas(self, deadline=None, commit=True):
        param = self.env['ir.config_parameter'].sudo()
        last = param.get_param('ksw_bas.last_sync_ledger')
        now = datetime.now()
        if last:
            since = min(datetime.fromisoformat(last),
                        now - timedelta(days=_LOOKBACK_DAYS))
        else:
            # bas9ss holds the current fiscal year only.
            since = datetime(now.year, 1, 1)
        since = since.replace(hour=0, minute=0, second=0, microsecond=0)

        try:
            conn = self._bas_connect()
            cursor = conn.cursor(as_dict=True)
        except Exception as e:
            _logger.error('KSW BAS ledger sync: connection failed: %s', e)
            return False
        try:
            cursor.execute("""
                SELECT FTYPE, FTYPE2, CODE2, NUMBER1, SERIAL, FDATE,
                       FCODE, TCODE, AMOUNT, ISNULL(TAX_AMOUNT, 0) AS TAX,
                       SELLER, SELLERNAME, ISNULL(DUTY_DAY, 0) AS DUTY_DAY,
                       NAME
                  FROM VOU10
                 WHERE (FCODE LIKE %s OR TCODE LIKE %s)
                   AND AMOUNT <> 0
                   AND FDATE >= %s
            """, (_ACCOUNT_LIKE, _ACCOUNT_LIKE, since))
            rows = cursor.fetchall()
        except Exception as e:
            _logger.error('KSW BAS ledger sync: query failed: %s', e)
            return False
        finally:
            conn.close()

        stamp = fields.Datetime.now()
        vals_list = []
        for r in rows:
            vals_list.append({
                'bas_key': self._make_key(r['FTYPE'], r['FTYPE2'], r['CODE2'],
                                          r['NUMBER1'], r['SERIAL']),
                'ftype': (r['FTYPE'] or '').strip(),
                'ftype2': int(r['FTYPE2'] or 0),
                'branch_code': (r['CODE2'] or '').strip(),
                'number': str(int(r['NUMBER1'] or 0)),
                'date': r['FDATE'].date() if r['FDATE'] else False,
                'debit_account': (r['FCODE'] or '').strip(),
                'credit_account': (r['TCODE'] or '').strip(),
                'amount': float(r['AMOUNT'] or 0),
                'tax_amount': float(r['TAX'] or 0),
                'seller_code': self._code(r['SELLER']),
                'seller_name': (r['SELLERNAME'] or '').strip(),
                'term_days': int(r['DUTY_DAY'] or 0),
                'description': (r['NAME'] or '').strip()[:200],
                'last_synced': stamp,
            })

        created, updated, done = self._bas_upsert(
            'bas_key', vals_list, self._COMPARE_FIELDS,
            deadline=deadline, commit=commit)
        removed = 0
        if done:
            # The window just read is complete: anything we hold in it that
            # BAS no longer returns was edited away or cancelled there.
            keys = {v['bas_key'] for v in vals_list}
            stale = self.search([('date', '>=', since.date())]).filtered(
                lambda l: l.bas_key not in keys)
            removed = len(stale)
            stale.unlink()
            param.set_param('ksw_bas.last_sync_ledger', now.isoformat())
            if commit:
                self.env.cr.commit()
        _logger.info(
            'KSW BAS: ledger %s — %d read, %d created, %d updated, %d removed '
            '(since %s)', 'complete' if done else 'INCOMPLETE (time budget)',
            len(rows), created, updated, removed, since.date())
        return done
