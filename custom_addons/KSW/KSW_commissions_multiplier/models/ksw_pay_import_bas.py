"""Driver trips weighted on the Odoo «الرد المضاعف».

BAS weights a load with ``cod10.FACTORE``, the customer's value *today*, so
every edit in BAS silently rewrites the past. Odoo keeps a dated history
(ksw.client.multiplier.rate), so from ``ODOO_MULTIPLIER_FROM_DEFAULT`` BAS is
asked only for the loads — driver, customer, day — and each one is weighted
with the value in force on its own day.

The base query is left as it is (cash bands, the «رد الفاتورة» window, the
counts the importer reports on). For the days on the Odoo basis this adds a
second query, per driver × customer × day, and swaps the FACTORE part of
``mult`` for the Odoo value. Both queries read the same lines with the same
filters, so the swap is exact.
"""
from collections import defaultdict

from odoo import api, fields, models

#: First day a load is weighted on the Odoo history instead of BAS FACTORE:
#: the day trips went back to «الرد المضاعف» (KSW_commissions 19.0.4.58.0).
ODOO_MULTIPLIER_FROM_DEFAULT = '2026-10-01'


class KswPayBatchOdooMultiplier(models.Model):
    _inherit = 'ksw.pay.batch'

    @api.model
    def _odoo_multiplier_from(self):
        value = self.env['ir.config_parameter'].sudo().get_param(
            'ksw_commissions.odoo_multiplier_from', ODOO_MULTIPLIER_FROM_DEFAULT)
        start = fields.Date.to_date(value or ODOO_MULTIPLIER_FROM_DEFAULT)
        # Never inside the «رد الفاتورة» window: there the invoice's own
        # value is what was paid.
        return max(start, self._invoice_factor_until())

    @api.model
    def _orood_codes(self):
        ICP = self.env['ir.config_parameter'].sudo()
        items = [c.strip() for c in (ICP.get_param(
            'ksw_commissions.orood_item_codes', '11032') or '').split(',')
            if c.strip()]
        ftypes = [c.strip() for c in (ICP.get_param(
            'ksw_commissions.orood_ftypes', '600') or '').split(',')
            if c.strip()]
        return items, ftypes

    @api.model
    def _bas_customer_loads(self, date_from, date_to, bas_code=None):
        """Water loads in ``[date_from, date_to)`` per driver × customer × day.

        ``[{'driver_cc', 'code', 'day', 'loads', 'bas_mult'}]``. Cash lines are
        left out when amount bands are configured, exactly as the base query
        weights them by band and not by customer.
        """
        items, ftypes = self._orood_codes()
        if not items or not ftypes:
            return []
        Band = self.env['ksw.pay.cash.band'].sudo()
        is_cash, cash = Band._cash_condition('s.FCODE')
        where_cash = ''
        if is_cash and Band._sql_case('s.AMOUNT'):
            # Till lines are weighted by amount band in the base query.
            # With no bands configured they are weighted like any other
            # line, so they stay on the Odoo basis here.
            where_cash = ' AND NOT ' + is_cash
        else:
            cash = []
        where_code = ''
        params = list(ftypes) + list(items) + [
            fields.Date.to_string(date_from), fields.Date.to_string(date_to)] + cash
        if bas_code:
            where_code = ' AND RTRIM(s.FCODE) = %s'
            params.append(bas_code)
        sql = (
            "SELECT h.COST_CENTER2 AS driver_cc, RTRIM(s.FCODE) AS code, "
            "       CONVERT(char(10), h.FDATE, 120) AS day, "
            "       COUNT(*) AS loads, SUM(ISNULL(c.FACTORE, 0)) AS bas_mult "
            "FROM vou10 h "
            "JOIN STR10 s ON s.FTYPE=h.FTYPE AND s.FTYPE2=h.FTYPE2 "
            "            AND s.CODE2=h.CODE2 AND s.NUMBER1=h.NUMBER1 "
            "LEFT JOIN cod10 c ON c.DCODE1 = s.FCODE "
            "WHERE h.FTYPE IN (" + ','.join(['%s'] * len(ftypes)) + ") "
            "  AND s.ICODE IN (" + ','.join(['%s'] * len(items)) + ") "
            "  AND h.FDATE >= %s AND h.FDATE < %s "
            "  AND h.COST_CENTER2 IS NOT NULL AND h.COST_CENTER2 <> ''"
            + where_cash + where_code +
            " GROUP BY h.COST_CENTER2, RTRIM(s.FCODE), CONVERT(char(10), h.FDATE, 120)"
        )
        conn = self.env['ksw.bas.connector']._bas_connect()
        try:
            cur = conn.cursor(as_dict=True)
            cur.execute(sql, tuple(params))
            return [{
                'driver_cc': row['driver_cc'] or '',
                'code': (row['code'] or '').strip(),
                'day': fields.Date.to_date(row['day'].strip()),
                'loads': int(row['loads'] or 0),
                'bas_mult': float(row['bas_mult'] or 0.0),
            } for row in cur.fetchall()]
        finally:
            conn.close()

    @api.model
    def _odoo_weights(self, lines):
        """{(code, day): multiplier} from the Odoo history; absent = no value."""
        codes = {l['code'] for l in lines if l['code']}
        clients = self.env['ksw.client.multiplier'].sudo().search(
            [('bas_code', 'in', list(codes))])
        by_code = {c.bas_code: c for c in clients}
        weights = {}
        for line in lines:
            key = (line['code'], line['day'])
            if key in weights:
                continue
            client = by_code.get(line['code'])
            rate = client._rate_on(line['day']) if client else None
            if rate:
                weights[key] = rate.multiplier
        return weights

    def _bas_fetch_orood(self, date_from, date_to):
        result = super()._bas_fetch_orood(date_from, date_to)
        start = max(fields.Date.to_date(date_from), self._odoo_multiplier_from())
        end = fields.Date.to_date(date_to)
        if start >= end or not result:
            return result
        lines = self._bas_customer_loads(start, end)
        weights = self._odoo_weights(lines)
        for line in lines:
            data = result.get(self._norm_cc(line['driver_cc']))
            if data is None:
                continue
            odoo = weights.get((line['code'], line['day']))
            data['mult'] += line['loads'] * (odoo or 0.0) - line['bas_mult']
            if odoo is None:
                data['no_odoo_value'] = data.get('no_odoo_value', 0) + line['loads']
        for data in result.values():
            # Float noise from the swap must not turn "no weight" into 1e-15.
            data['mult'] = round(data['mult'], 4)
        return result
