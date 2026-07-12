import logging
from datetime import datetime, timedelta
from odoo import models, fields, api

_logger = logging.getLogger(__name__)

_SYNC_DAYS = 90


class BASPayment(models.Model):
    _name = 'ksw.bas.payment'
    _description = 'BAS Payment Voucher'
    _inherit = ['ksw.bas.connector']
    _order = 'payment_date desc'

    bas_key = fields.Char('BAS Key', readonly=True, index=True)
    ftype = fields.Char('Voucher Type', readonly=True)
    branch_code = fields.Char('Branch', readonly=True)
    number = fields.Char('Voucher No.', readonly=True)
    payment_date = fields.Datetime('Date', readonly=True)
    from_account = fields.Char('From Account', readonly=True)
    to_account = fields.Char('To Account', readonly=True)
    amount = fields.Float('Amount', readonly=True, digits=(16, 2))
    pay_mode = fields.Char('Payment Mode', readonly=True)
    remark = fields.Char('Description', readonly=True)
    last_synced = fields.Datetime('Last Synced', readonly=True)

    @staticmethod
    def _make_key(ftype, ftype2, code2, number1):
        return f'{ftype}_{ftype2}_{code2}_{int(number1 or 0)}'

    @api.model
    def sync_from_bas(self):
        param = self.env['ir.config_parameter'].sudo()
        last_sync_str = param.get_param('ksw_bas.last_sync_payment')
        if last_sync_str:
            since = datetime.fromisoformat(last_sync_str)
        else:
            since = datetime.now() - timedelta(days=_SYNC_DAYS)

        try:
            conn = self._bas_connect()
            cursor = conn.cursor(as_dict=True)
        except Exception as e:
            _logger.error('KSW BAS payment sync: connection failed: %s', e)
            return

        try:
            # FTYPE=018 = cash receipt, FTYPE=019 = cash payment, FTYPE=015 = bank receipt
            # Only one row per voucher (FCODE or TCODE populated, not both)
            # We pick the row that has a non-empty TCODE (the receivable/payable side)
            cursor.execute("""
                SELECT
                    FTYPE, FTYPE2, CODE2, NUMBER1,
                    FDATE, FCODE, TCODE, AMOUNT, PAYMODE, REMARK
                FROM VOU10
                WHERE FTYPE IN ('018', '019', '015')
                AND FDATE >= %s
                AND (TCODE IS NOT NULL AND TCODE != '')
                ORDER BY FDATE DESC
            """, (since,))
            rows = cursor.fetchall()
        except Exception as e:
            _logger.error('KSW BAS payment sync: query failed: %s', e)
            conn.close()
            return
        finally:
            conn.close()

        now = fields.Datetime.now()
        existing_keys = {r.bas_key: r for r in self.search([])}

        ftype_labels = {'018': 'Receipt', '019': 'Payment', '015': 'Bank Receipt'}

        for row in rows:
            key = self._make_key(row['FTYPE'], row['FTYPE2'], row['CODE2'], row['NUMBER1'])
            vals = {
                'bas_key': key,
                'ftype': ftype_labels.get(row['FTYPE'], row['FTYPE']),
                'branch_code': (row['CODE2'] or '').strip(),
                'number': str(int(row['NUMBER1'] or 0)),
                'payment_date': row['FDATE'],
                'from_account': (row['FCODE'] or '').strip(),
                'to_account': (row['TCODE'] or '').strip(),
                'amount': float(row['AMOUNT'] or 0),
                'pay_mode': (row['PAYMODE'] or '').strip(),
                'remark': (row['REMARK'] or '').strip(),
                'last_synced': now,
            }
            if key in existing_keys:
                existing_keys[key].write(vals)
            else:
                self.create(vals)

        param.set_param('ksw_bas.last_sync_payment', datetime.now().isoformat())
        _logger.info('KSW BAS: synced %d payment vouchers (since %s)', len(rows), since.date())
