import logging
from datetime import datetime, timedelta
from odoo import models, fields, api

_logger = logging.getLogger(__name__)

# FTYPE codes that represent sales invoices in BAS
_SALES_FTYPES = (
    ('001', 0),   # Standard sales invoice
    ('001', 2),   # Sales invoice variant
    ('001', 3),   # Sales invoice variant
    ('600', 2),   # POS sale
    ('600', 3),   # POS sale variant
)
_SYNC_DAYS = 90  # Sync last N days on first run; incremental after
# See KSW_ext_sync/models/bas_payment.py _LOOKBACK_DAYS for why this
# exists: BAS allows backdated postings, so a pure forward watermark
# permanently misses entries dated before it but posted after it.
_LOOKBACK_DAYS = 30


def _fiscal_year_start(now):
    """`bas9ss` only ever holds the current fiscal year (see
    BAS_DATABASE_REFERENCE.md — closed years live in separate archive
    databases this connector doesn't reach). A first-run sync should
    therefore always cover from Jan 1 of the current year, not just a
    trailing N-day window — needed for whole-year AR aging (see
    KSW_commissions "Collection Target from BAS Aging"), and it's free:
    querying earlier than Jan 1 would just return nothing anyway.
    """
    return min(now - timedelta(days=_SYNC_DAYS), datetime(now.year, 1, 1))


class BASInvoice(models.Model):
    _name = 'ksw.bas.invoice'
    _description = 'BAS Sales Invoice'
    _inherit = ['ksw.bas.connector']
    _order = 'invoice_date desc'

    bas_key = fields.Char('BAS Key', readonly=True, index=True)
    ftype = fields.Char('Doc Type', readonly=True)
    branch_code = fields.Char('Branch', readonly=True)
    number = fields.Char('Invoice No.', readonly=True)
    invoice_date = fields.Date('Date', readonly=True)
    from_account = fields.Char('From Account', readonly=True)
    to_account = fields.Char('To Account', readonly=True)
    subtotal = fields.Float('Subtotal', readonly=True, digits=(16, 2))
    tax_amount = fields.Float('VAT', readonly=True, digits=(16, 2))
    total = fields.Float('Total', compute='_compute_total', store=True, digits=(16, 2))
    status = fields.Char('Status', readonly=True)
    last_synced = fields.Datetime('Last Synced', readonly=True)

    @api.depends('subtotal', 'tax_amount')
    def _compute_total(self):
        for rec in self:
            rec.total = (rec.subtotal or 0) + (rec.tax_amount or 0)

    @staticmethod
    def _make_key(ftype, ftype2, code2, number1):
        return f'{ftype}_{ftype2}_{code2}_{int(number1 or 0)}'

    @api.model
    def sync_from_bas(self):
        param = self.env['ir.config_parameter'].sudo()
        last_sync_str = param.get_param('ksw_bas.last_sync_invoice')
        now = datetime.now()
        if last_sync_str:
            since = min(
                datetime.fromisoformat(last_sync_str),
                now - timedelta(days=_LOOKBACK_DAYS))
        else:
            since = _fiscal_year_start(now)

        try:
            conn = self._bas_connect()
            cursor = conn.cursor(as_dict=True)
        except Exception as e:
            _logger.error('KSW BAS invoice sync: connection failed: %s', e)
            return

        try:
            # FTYPE=600 (POS): amounts from STR10 lines
            cursor.execute("""
                SELECT
                    h.FTYPE, h.FTYPE2, h.CODE2, h.NUMBER1,
                    h.DATE1, h.FCODE, h.TCODE,
                    ISNULL(h.TAXES_AMOUNT, 0) AS TAXES_AMOUNT,
                    CAST(h.STATUS_VOU AS NVARCHAR(50)) AS STATUS_VOU,
                    ISNULL(SUM(s.AMOUNT), 0) AS SUBTOTAL
                FROM INV10 h
                LEFT JOIN STR10 s
                    ON s.FTYPE = h.FTYPE AND s.FTYPE2 = h.FTYPE2
                    AND s.CODE2 = h.CODE2 AND s.NUMBER1 = h.NUMBER1
                WHERE h.FTYPE = '600'
                AND h.DATE1 >= %s
                GROUP BY h.FTYPE, h.FTYPE2, h.CODE2, h.NUMBER1,
                         h.DATE1, h.FCODE, h.TCODE, h.TAXES_AMOUNT, h.STATUS_VOU
                ORDER BY h.DATE1 DESC
            """, (since,))
            rows_pos = cursor.fetchall()

            # FTYPE=001 (standard invoice): total from VOU10 debit line (FCODE set)
            cursor.execute("""
                SELECT
                    h.FTYPE, h.FTYPE2, h.CODE2, h.NUMBER1,
                    h.DATE1, h.FCODE, h.TCODE,
                    ISNULL(h.TAXES_AMOUNT, 0) AS TAXES_AMOUNT,
                    CAST(h.STATUS_VOU AS NVARCHAR(50)) AS STATUS_VOU,
                    ISNULL(
                        (SELECT MAX(v2.AMOUNT) FROM VOU10 v2
                         WHERE v2.FTYPE=h.FTYPE AND v2.FTYPE2=h.FTYPE2
                         AND v2.CODE2=h.CODE2 AND v2.NUMBER1=h.NUMBER1
                         AND v2.FCODE IS NOT NULL AND v2.FCODE != ''),
                    0) AS SUBTOTAL
                FROM INV10 h
                WHERE h.FTYPE = '001'
                AND h.DATE1 >= %s
                ORDER BY h.DATE1 DESC
            """, (since,))
            rows_std = cursor.fetchall()

            rows = rows_pos + rows_std
        except Exception as e:
            _logger.error('KSW BAS invoice sync: query failed: %s', e)
            conn.close()
            return
        finally:
            conn.close()

        now = fields.Datetime.now()
        existing_keys = {
            r.bas_key: r for r in self.search([])
        }

        for row in rows:
            key = self._make_key(row['FTYPE'], row['FTYPE2'], row['CODE2'], row['NUMBER1'])
            inv_date = row['DATE1'].date() if row['DATE1'] else None
            vals = {
                'bas_key': key,
                'ftype': f"{row['FTYPE']}/{row['FTYPE2']}",
                'branch_code': (row['CODE2'] or '').strip(),
                'number': str(int(row['NUMBER1'] or 0)),
                'invoice_date': inv_date,
                'from_account': (row['FCODE'] or '').strip(),
                'to_account': (row['TCODE'] or '').strip(),
                'subtotal': float(row['SUBTOTAL'] or 0),
                'tax_amount': float(row['TAXES_AMOUNT'] or 0),
                'status': (row['STATUS_VOU'] or '').strip(),
                'last_synced': now,
            }
            if key in existing_keys:
                existing_keys[key].write(vals)
            else:
                self.create(vals)

        param.set_param('ksw_bas.last_sync_invoice', datetime.now().isoformat())
        _logger.info('KSW BAS: synced %d invoices (since %s)', len(rows), since.date())
