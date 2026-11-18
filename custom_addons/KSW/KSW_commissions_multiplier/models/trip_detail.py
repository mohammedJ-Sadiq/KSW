"""Trip Details: every load behind a Driver Trips line, and how it was weighted.

The tier explanation (``ksw.pay.entry.action_explain``) starts from the
weighted quantity. This shows where that quantity comes from: one row per BAS
load (day, document, truck, customer, amount), the basis its weight was taken
from and the weight itself, adding up to the line.

Each load is classified exactly as the importer weights it
(``ksw.pay.batch._bas_fetch_orood`` and this module's override), in the same
order:

1. a till (``ksw.pay.cash.band._cash_condition``) with bands configured:
   the amount band, on every date;
2. inside the «رد الفاتورة» window: ``vou10.PHRDIS``;
3. from ``_odoo_multiplier_from``: the Odoo «الرد المضاعف» on that day;
4. before that: BAS ``cod10.FACTORE``.

The total is compared with the line's stored quantity. They differ only when
BAS changed after the import (a load added, edited or deleted); the dialog
says so instead of hiding it.
"""
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

BASES = [
    ('odoo', 'Odoo «الرد المضاعف»'),
    ('cash', 'Cash sale: amount band'),
    ('invoice', '«رد الفاتورة» on the invoice'),
    ('bas', 'BAS «الرد المضاعف»'),
    ('none', 'No multiplier recorded'),
]


class KswPayBatchTripLines(models.Model):
    _inherit = 'ksw.pay.batch'

    @api.model
    def _bas_trip_lines(self, date_from, date_to):
        """Every water load in ``[date_from, date_to)``, one dict per BAS line."""
        items, ftypes = self._orood_codes()
        if not items or not ftypes:
            return []
        Band = self.env['ksw.pay.cash.band'].sudo()
        is_cash, cash = Band._cash_condition('s.FCODE')
        band_case = Band._sql_case('s.AMOUNT') if is_cash else None
        if not band_case:
            is_cash, cash, band_case = '1 = 0', [], '0'
        sql = (
            "SELECT h.COST_CENTER2 AS driver_cc, h.COST_CENTER AS truck, "
            "       CONVERT(char(10), h.FDATE, 120) AS day, "
            "       RTRIM(h.CODE2) AS branch, h.NUMBER1 AS number, "
            "       RTRIM(s.FCODE) AS code, c.DNAME AS customer, "
            "       s.AMOUNT AS amount, c.FACTORE AS factore, h.PHRDIS AS phrdis, "
            "       CASE WHEN " + is_cash + " THEN 1 ELSE 0 END AS is_cash, "
            "       " + band_case + " AS band "
            "FROM vou10 h "
            "JOIN STR10 s ON s.FTYPE=h.FTYPE AND s.FTYPE2=h.FTYPE2 "
            "            AND s.CODE2=h.CODE2 AND s.NUMBER1=h.NUMBER1 "
            "LEFT JOIN cod10 c ON c.DCODE1 = s.FCODE "
            "WHERE h.FTYPE IN (" + ','.join(['%s'] * len(ftypes)) + ") "
            "  AND s.ICODE IN (" + ','.join(['%s'] * len(items)) + ") "
            "  AND h.FDATE >= %s AND h.FDATE < %s "
            "  AND h.COST_CENTER2 IS NOT NULL AND h.COST_CENTER2 <> '' "
            "ORDER BY h.FDATE, h.CODE2, h.NUMBER1"
        )
        params = cash + list(ftypes) + list(items) + [
            fields.Date.to_string(date_from), fields.Date.to_string(date_to)]
        conn = self.env['ksw.bas.connector']._bas_connect()
        try:
            cur = conn.cursor(as_dict=True)
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        finally:
            conn.close()
        return [{
            'driver_cc': r['driver_cc'] or '',
            'truck': (r['truck'] or '').strip(),
            'day': fields.Date.to_date(r['day'].strip()),
            'document': '%s/%s' % (r['branch'] or '', int(r['number'] or 0)),
            'code': (r['code'] or '').strip(),
            'customer': (r['customer'] or '').strip(),
            'amount': float(r['amount'] or 0.0),
            'factore': float(r['factore'] or 0.0),
            'phrdis': float(r['phrdis'] or 0.0),
            'is_cash': bool(r['is_cash']),
            'band': float(r['band'] or 0.0),
        } for r in rows]

    @api.model
    def _weigh_trip_lines(self, lines):
        """Add ``basis`` / ``weight`` / ``source`` to each line, as the importer weighs it."""
        inv_from = self._invoice_factor_cutover()
        inv_until = self._invoice_factor_until()
        odoo_from = self._odoo_multiplier_from()
        odoo_lines = [l for l in lines if not l['is_cash'] and l['day'] >= odoo_from]
        clients = self.env['ksw.client.multiplier'].sudo().search(
            [('bas_code', 'in', list({l['code'] for l in odoo_lines}))])
        by_code = {c.bas_code: c for c in clients}
        bands = self.env['ksw.pay.cash.band'].sudo().search([], order='amount_from desc')
        for line in lines:
            if line['is_cash']:
                band = next((b for b in bands if line['amount'] >= b.amount_from and (
                    not b.amount_to or line['amount'] <= b.amount_to)), None)
                line.update(basis='cash', weight=line['band'], source=(
                    _('Band %(f).0f–%(t)s SAR', f=band.amount_from,
                      t=('%.0f' % band.amount_to) if band.amount_to else '…')
                    if band else _('Below every band')))
            elif inv_from <= line['day'] < inv_until:
                line.update(basis='invoice' if line['phrdis'] else 'none',
                            weight=line['phrdis'], source=_('vou10.PHRDIS'))
            elif line['day'] >= odoo_from:
                client = by_code.get(line['code'])
                rate = client._rate_on(line['day']) if client else None
                if rate:
                    source = (_('%(req)s from %(day)s', req=rate.request_id.name,
                                day=rate.date_from) if rate.request_id
                              else _('From %(day)s', day=rate.date_from) if rate.date_from
                              else _('Opening value'))
                    line.update(basis='odoo', weight=rate.multiplier, source=source)
                else:
                    line.update(basis='none', weight=0.0,
                                source=_('No client multiplier in Odoo'))
            else:
                line.update(basis='bas' if line['factore'] else 'none',
                            weight=line['factore'], source=_('cod10.FACTORE (today)'))
            if line['code'] in by_code:
                line['customer'] = by_code[line['code']].name
        return lines


class KswPayEntryTripDetail(models.Model):
    _inherit = 'ksw.pay.entry'

    x_component_importer = fields.Selection(related='component_id.importer')

    def action_trip_details(self):
        self.ensure_one()
        self.check_access('read')
        entry = self.sudo()
        if entry.component_id.importer != 'bas_trips':
            raise UserError(_('Trip Details are only available for driver trips imported from BAS.'))
        Batch = self.env['ksw.pay.batch'].sudo()
        month_start, month_end = entry._month_bounds()
        day_from = entry.x_window_from or month_start
        day_to = entry.x_window_to or month_end
        keys = set(Batch._bas_cost_centres(entry.employee_id))
        if not keys:
            raise UserError(_('%(emp)s has no BAS cost centre, so there are no loads to show.',
                              emp=entry.employee_id.name))
        lines = [l for l in Batch._bas_trip_lines(day_from, day_to + timedelta(days=1))
                 if Batch._norm_cc(l['driver_cc']) in keys]
        Batch._weigh_trip_lines(lines)
        detail = self.env['ksw.pay.trip.detail'].create({
            'entry_id': entry.id,
            'date_from': day_from,
            'date_to': day_to,
            'line_ids': [(0, 0, {
                'day': l['day'], 'document': l['document'], 'truck': l['truck'],
                'customer_code': l['code'], 'customer': l['customer'],
                'amount': l['amount'], 'basis': l['basis'], 'weight': l['weight'],
                'source': l['source'],
            }) for l in lines],
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _('Trip Details: %(emp)s', emp=entry.employee_id.name),
            'res_model': 'ksw.pay.trip.detail',
            'res_id': detail.id,
            'view_mode': 'form',
            'target': 'new',
        }


class KswPayTripDetail(models.TransientModel):
    _name = 'ksw.pay.trip.detail'
    _description = 'Driver Trip Details'

    entry_id = fields.Many2one('ksw.pay.entry', readonly=True, ondelete='cascade')
    employee_id = fields.Many2one(related='entry_id.employee_id', string='Driver')
    period = fields.Date(related='entry_id.period')
    date_from = fields.Date(string='From', readonly=True)
    date_to = fields.Date(string='To', readonly=True)
    stored_quantity = fields.Float(related='entry_id.quantity', string='On the Line',
                                   digits=(16, 2))
    stored_loads = fields.Float(related='entry_id.quantity_ref', string='Loads on the Line')
    line_ids = fields.One2many('ksw.pay.trip.detail.line', 'detail_id', readonly=True)
    loads = fields.Integer(compute='_compute_totals', string='Loads (عدد الردود)')
    weighted = fields.Float(compute='_compute_totals', digits=(16, 2),
                            string='Multiplied Trips (الرد المضاعف)')
    difference = fields.Float(compute='_compute_totals', digits=(16, 2))
    summary = fields.Html(compute='_compute_totals', sanitize=False)

    @api.depends('line_ids.weight', 'stored_quantity')
    def _compute_totals(self):
        labels = dict(BASES)
        for rec in self:
            rec.loads = len(rec.line_ids)
            rec.weighted = round(sum(rec.line_ids.mapped('weight')), 2)
            rec.difference = round(rec.weighted - (rec.stored_quantity or 0.0), 2)
            rows = ''
            for key, _label in BASES:
                part = rec.line_ids.filtered(lambda l, k=key: l.basis == k)
                if part:
                    rows += '<tr><td>%s</td><td class="text-end">%d</td><td class="text-end">%.2f</td></tr>' % (
                        labels[key], len(part), sum(part.mapped('weight')))
            rec.summary = (
                '<table class="table table-sm o_ksw_trip_summary"><thead><tr><th>%s</th>'
                '<th class="text-end">%s</th><th class="text-end">%s</th></tr></thead>'
                '<tbody>%s</tbody></table>' % (
                    _('Weighted on'), _('Loads'), _('Multiplied trips'), rows))

    def action_open_list(self):
        """The same rows in a list: group by customer / day / basis, export."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Trip Details: %(emp)s', emp=self.employee_id.name),
            'res_model': 'ksw.pay.trip.detail.line',
            'view_mode': 'list',
            'domain': [('detail_id', '=', self.id)],
            'context': {'search_default_g_customer': 1},
        }


class KswPayTripDetailLine(models.TransientModel):
    _name = 'ksw.pay.trip.detail.line'
    _description = 'Driver Trip Detail Line'
    _order = 'day, document'

    detail_id = fields.Many2one('ksw.pay.trip.detail', required=True, ondelete='cascade')
    day = fields.Date(readonly=True)
    document = fields.Char(string='BAS Document', readonly=True,
                           help='Branch / number of the delivery document in BAS (vou10).')
    truck = fields.Char(readonly=True)
    customer_code = fields.Char(string='Account', readonly=True)
    customer = fields.Char(readonly=True)
    amount = fields.Float(readonly=True, digits=(16, 2))
    basis = fields.Selection(BASES, string='Weighted On', readonly=True)
    weight = fields.Float(string='Multiplier', readonly=True, digits=(16, 2))
    source = fields.Char(string='Where the Multiplier Comes From', readonly=True)
