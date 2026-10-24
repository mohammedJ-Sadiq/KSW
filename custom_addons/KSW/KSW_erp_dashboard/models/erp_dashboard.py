"""Data for the Accounting and Inventory overview pages.

Modelled on what SAP Fiori overview pages (AR/AP Overview, Cash Position,
Inventory Turnover, Dead Stock, Slow-Moving Materials), Oracle Fusion infolets
(Payables / Receivables / Cash Management, DSO, DPO, DIO) and Odoo Enterprise
(journal cards, operation cards) put first: money position, what is owed and
overdue, how the period is going, stock value, stock that does not move, stock
about to run out, and the work waiting. Every figure comes with the domain of
the records behind it, so a card always opens what it counts.

Everything is read as the calling user (record rules apply); only the unit
cost of an item is read with sudo.
"""
from collections import defaultdict
from datetime import date, datetime, time, timedelta

import pytz

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models

_INCOME = ('income', 'income_other')
_EXPENSE = ('expense', 'expense_depreciation', 'expense_direct_cost')


def _period(period, today):
    """(start, end, previous start, previous end, label) for this / last month, quarter, year."""
    if period == 'last_month':
        start = (today.replace(day=1) - relativedelta(months=1))
        end = today.replace(day=1) - timedelta(days=1)
        prev = start - relativedelta(months=1)
        return start, end, prev, start - timedelta(days=1)
    if period == 'quarter':
        q = (today.month - 1) // 3
        start = date(today.year, 3 * q + 1, 1)
        prev = start - relativedelta(months=3)
        return start, today, prev, start - timedelta(days=1)
    if period == 'year':
        start = date(today.year, 1, 1)
        return start, today, date(today.year - 1, 1, 1), date(today.year - 1, today.month, today.day)
    start = today.replace(day=1)
    prev = start - relativedelta(months=1)
    return start, today, prev, start - timedelta(days=1)


def _trend_buckets(period, today):
    """The chart's bars for a period: (unit, [(label, first day, last day)]).

    A month is shown by day, a quarter by week (seven-day steps from its first
    day), a year by month, so the chart always covers the period the page is on.
    """
    start, end, _ps, _pe = _period(period, today)
    buckets = []
    if period == 'year':
        month = start
        while month <= end:
            nxt = month + relativedelta(months=1)
            buckets.append((month.strftime('%Y-%m'), month, min(nxt - timedelta(days=1), end)))
            month = nxt
        return 'month', buckets
    step = 7 if period == 'quarter' else 1
    day = start
    while day <= end:
        last = min(day + timedelta(days=step - 1), end)
        buckets.append((day.strftime('%d/%m') if step > 1 else day.strftime('%d'), day, last))
        day = last + timedelta(days=1)
    return ('week' if step > 1 else 'day'), buckets


def _sum_by_bucket(buckets, by_day):
    """Spread {date: amount} over the buckets."""
    return [sum(v for d, v in by_day.items() if a <= d <= b) for _l, a, b in buckets]


class KswErpDashboard(models.AbstractModel):
    _name = 'ksw.erp.dashboard'
    _description = 'Accounting and Inventory Overview'

    @api.model
    def _datetime_span(self, start, end, field='date'):
        """Domain on a Datetime field for local days start..end (stored UTC; Riyadh is +3)."""
        tz = pytz.timezone(self.env.context.get('tz') or self.env.user.tz or 'UTC')

        def utc(day):
            return tz.localize(datetime.combine(day, time.min)).astimezone(pytz.utc).replace(tzinfo=None)
        return [(field, '>=', utc(start)), (field, '<', utc(end + timedelta(days=1)))]

    # ------------------------------------------------------------------
    # Accounting
    # ------------------------------------------------------------------
    @api.model
    def get_finance_data(self, period='month'):
        company = self.env.company
        today = fields.Date.context_today(self)
        start, end, pstart, pend = _period(period, today)
        AML = self.env['account.move.line']
        base = [('parent_state', '=', 'posted'), ('company_id', '=', company.id)]
        cur = company.currency_id

        def total(domain, field='balance'):
            return AML._read_group(base + domain, [], [f'{field}:sum'])[0][0] or 0.0

        # Cash position: every bank and cash journal's account.
        journals = self.env['account.journal'].search(
            [('company_id', '=', company.id), ('type', 'in', ('bank', 'cash')), ('default_account_id', '!=', False)])
        by_account = dict(AML._read_group(base + [('account_id', 'in', journals.default_account_id.ids)],
                                          ['account_id'], ['balance:sum']))
        cash_rows = sorted(
            ({'name': j.name, 'type': j.type, 'amount': by_account.get(j.default_account_id, 0.0),
              'domain': [('account_id', '=', j.default_account_id.id)] + base} for j in journals),
            key=lambda r: -abs(r['amount']))
        cash_total = sum(r['amount'] for r in cash_rows)

        def aging(account_type, sign):
            dom = [('account_id.account_type', '=', account_type), ('reconciled', '=', False),
                   ('amount_residual', '!=', 0)]
            # Days overdue = today - due date. (key, label, fewest days, most days)
            buckets = [('1_30', self.env._('1–30 days'), 1, 30), ('31_60', self.env._('31–60 days'), 31, 60),
                       ('61_90', self.env._('61–90 days'), 61, 90), ('90p', self.env._('Over 90 days'), 91, None)]
            not_due = dom + ['|', ('date_maturity', '>=', today), ('date_maturity', '=', False)]
            out = [{'key': 'current', 'label': self.env._('Not due'),
                    'amount': sign * total(not_due, 'amount_residual'), 'domain': base + not_due}]
            for key, label, low, high in buckets:
                d = dom + [('date_maturity', '<=', today - timedelta(days=low))]
                if high:
                    d.append(('date_maturity', '>=', today - timedelta(days=high)))
                out.append({'key': key, 'label': label,
                            'amount': sign * total(d, 'amount_residual'), 'domain': base + d})
            open_total = sum(b['amount'] for b in out)
            overdue = sum(b['amount'] for b in out if b['key'] != 'current')
            top = AML._read_group(base + dom + [('date_maturity', '<', today), ('partner_id', '!=', False)],
                                  ['partner_id'], ['amount_residual:sum'],
                                  order='amount_residual:sum desc' if sign > 0 else 'amount_residual:sum asc',
                                  limit=5)
            return {
                'open': open_total, 'overdue': overdue, 'buckets': out,
                'domain_open': base + dom, 'domain_overdue': base + dom + [('date_maturity', '<', today)],
                'top': [{'name': p.display_name, 'amount': sign * a,
                         'domain': base + dom + [('partner_id', '=', p.id), ('date_maturity', '<', today)]}
                        for p, a in top],
            }

        receivable = aging('asset_receivable', 1)
        payable = aging('liability_payable', -1)
        d90 = today - timedelta(days=90)
        sales_90 = -total([('account_id.account_type', 'in', _INCOME), ('date', '>', d90)])
        purchases_90 = total([('account_id.account_type', 'in', _EXPENSE), ('date', '>', d90)])
        dso = receivable['open'] / sales_90 * 90 if sales_90 > 0 else 0
        dpo = payable['open'] / purchases_90 * 90 if purchases_90 > 0 else 0

        def pl(a, b):
            rev = -total([('account_id.account_type', 'in', _INCOME), ('date', '>=', a), ('date', '<=', b)])
            exp = total([('account_id.account_type', 'in', _EXPENSE), ('date', '>=', a), ('date', '<=', b)])
            return {'revenue': rev, 'expenses': exp, 'profit': rev - exp}

        current, previous = pl(start, end), pl(pstart, pend)
        ytd = pl(date(today.year, 1, 1), today)

        # Revenue and expenses across the period: by day, week or month.
        trend_unit, buckets = _trend_buckets(period, today)

        def by_day(types):
            return {d: amt for d, amt in AML._read_group(
                base + [('account_id.account_type', 'in', types), ('date', '>=', start), ('date', '<=', end)],
                ['date:day'], ['balance:sum'])}
        revenue = _sum_by_bucket(buckets, {d: -v for d, v in by_day(_INCOME).items()})
        expenses = _sum_by_bucket(buckets, by_day(_EXPENSE))
        trend = [{'label': label, 'revenue': r, 'expenses': e, 'profit': r - e}
                 for (label, _a, _b), r, e in zip(buckets, revenue, expenses)]

        # Where the expenses went: cost centres (BAS vehicles, departments, branches).
        cc = []
        if 'x_cost_centre_id' in AML._fields:
            rows = AML._read_group(base + [('account_id.account_type', 'in', _EXPENSE), ('date', '>=', start),
                                           ('date', '<=', end), ('x_cost_centre_id', '!=', False)],
                                   ['x_cost_centre_id'], ['balance:sum'], order='balance:sum desc', limit=8)
            cc = [{'name': a.display_name, 'amount': amt,
                   'domain': base + [('account_id.account_type', 'in', _EXPENSE), ('date', '>=', start),
                                     ('date', '<=', end), ('x_cost_centre_id', '=', a.id)]} for a, amt in rows]

        Move = self.env['account.move']
        mbase = [('company_id', '=', company.id)]
        week = today + timedelta(days=7)
        todo = [
            {'key': 'drafts', 'label': self.env._('Draft entries to post'), 'model': 'account.move',
             'domain': mbase + [('state', '=', 'draft')]},
            {'key': 'bills_due', 'label': self.env._('Bills due within 7 days'), 'model': 'account.move',
             'domain': mbase + [('move_type', '=', 'in_invoice'), ('state', '=', 'posted'),
                                ('payment_state', 'in', ('not_paid', 'partial')),
                                ('invoice_date_due', '<=', week)]},
            {'key': 'invoices_overdue', 'label': self.env._('Customer invoices overdue'), 'model': 'account.move',
             'domain': mbase + [('move_type', '=', 'out_invoice'), ('state', '=', 'posted'),
                                ('payment_state', 'in', ('not_paid', 'partial')),
                                ('invoice_date_due', '<', today)]},
            {'key': 'payments_open', 'label': self.env._('Payments not fully matched'), 'model': 'account.payment',
             'domain': [('company_id', '=', company.id), ('state', '=', 'in_process')]},
        ]
        for t in todo:
            t['count'] = self.env[t['model']].search_count(t['domain'])

        return {
            'currency': {'symbol': cur.symbol, 'position': cur.position, 'digits': cur.decimal_places},
            'period': {'start': str(start), 'end': str(end)},
            'cash': {'total': cash_total, 'rows': cash_rows[:8],
                     'domain': base + [('account_id', 'in', journals.default_account_id.ids)]},
            'receivable': receivable, 'payable': payable,
            'dso': round(dso), 'dpo': round(dpo),
            'pl': {'current': current, 'previous': previous, 'ytd': ytd},
            'trend': trend, 'trend_unit': trend_unit, 'cost_centres': cc, 'todo': todo,
        }

    # ------------------------------------------------------------------
    # Inventory
    # ------------------------------------------------------------------
    @api.model
    def get_inventory_data(self, period='month'):
        company = self.env.company
        today = fields.Date.context_today(self)
        start, end, _ps, _pe = _period(period, today)
        Quant = self.env['stock.quant']
        Move = self.env['stock.move']
        qbase = [('location_id.usage', '=', 'internal'), ('company_id', '=', company.id),
                 ('product_id.is_storable', '=', True)]
        quants = Quant._read_group(qbase, ['product_id', 'location_id'], ['quantity:sum'])
        cost = {p.id: p.with_company(company).sudo().standard_price
                for p in self.env['product.product'].browse({p.id for p, _l, _q in quants})}

        def unit_cost(product):
            if product.id not in cost:
                cost[product.id] = product.with_company(company).sudo().standard_price
            return cost[product.id]
        value_by_cat, value_by_wh = defaultdict(float), defaultdict(float)
        on_hand = defaultdict(float)
        negative = set()
        for product, location, qty in quants:
            on_hand[product] += qty
            if qty < 0:
                negative.add(product.id)
            value = qty * cost.get(product.id, 0.0)
            value_by_cat[product.categ_id] += value
            value_by_wh[location.warehouse_id] += value
        stock_value = sum(value_by_cat.values())
        in_stock = [p for p, q in on_hand.items() if q > 0]

        # Movement out of stock (issues, deliveries, consumption) per product.
        out_dom = [('state', '=', 'done'), ('company_id', '=', company.id), ('product_id.is_storable', '=', True),
                   ('location_id.usage', '=', 'internal'), ('location_dest_id.usage', '!=', 'internal')]
        last_out = dict(Move._read_group(out_dom, ['product_id'], ['date:max']))
        used_90 = dict(Move._read_group(out_dom + [('date', '>=', today - timedelta(days=90))],
                                        ['product_id'], ['product_uom_qty:sum']))
        dead, slow = [], []
        for p in in_stock:
            last = last_out.get(p)
            days = (today - last.date()).days if last else None
            row = {'id': p.id, 'name': p.display_name, 'qty': on_hand[p], 'value': on_hand[p] * cost.get(p.id, 0.0),
                   'idle_days': days}
            if days is None or days > 180:
                dead.append(row)
            elif days > 90:
                slow.append(row)
        dead.sort(key=lambda r: -r['value'])
        slow.sort(key=lambda r: -r['value'])

        # Days of supply: on hand ÷ average daily use over 90 days. Short ones run out first.
        cover = []
        for p, used in used_90.items():
            daily = used / 90.0
            if daily > 0:
                cover.append({'id': p.id, 'name': p.display_name, 'qty': on_hand.get(p, 0.0),
                              'days': round(on_hand.get(p, 0.0) / daily)})
        cover.sort(key=lambda r: r['days'])
        out_of_stock = [r for r in cover if r['qty'] <= 0]
        running_low = [r for r in cover if 0 < r['qty'] and r['days'] <= 30]

        below_min = []
        if 'stock.warehouse.orderpoint' in self.env:
            for op in self.env['stock.warehouse.orderpoint'].search([('company_id', '=', company.id),
                                                                     ('trigger', '!=', False)], limit=500):
                if op.qty_on_hand < op.product_min_qty:
                    below_min.append(op.id)

        # Stock turnover (a year): cost of what went out ÷ stock value now.
        used_value_365 = 0.0
        for product, qty in Move._read_group(out_dom + [('date', '>=', today - timedelta(days=365))],
                                             ['product_id'], ['product_uom_qty:sum']):
            used_value_365 += qty * unit_cost(product)
        turnover = used_value_365 / stock_value if stock_value else 0
        dio = 365 / turnover if turnover else 0

        # Work waiting, per operation type.
        Picking = self.env['stock.picking']
        ops = []
        now = fields.Datetime.now()
        for ptype in self.env['stock.picking.type'].search([('company_id', '=', company.id), ('active', '=', True)]):
            pbase = [('picking_type_id', '=', ptype.id)]
            ready = Picking.search_count(pbase + [('state', '=', 'assigned')])
            waiting = Picking.search_count(pbase + [('state', 'in', ('confirmed', 'waiting'))])
            late = Picking.search_count(pbase + [('state', 'in', ('assigned', 'waiting', 'confirmed')),
                                                 ('scheduled_date', '<', now)])
            if ready or waiting or late:
                ops.append({'id': ptype.id, 'name': f'{ptype.warehouse_id.code or ""} {ptype.name}'.strip(),
                            'ready': ready, 'waiting': waiting, 'late': late})
        ops.sort(key=lambda o: -(o['late'] * 1000 + o['ready'] + o['waiting']))

        # Issues to vehicles / cost centres in the period, by value.
        # stock.move only carries analytic_distribution when an analytic stock
        # module is installed (on dev, OCA stock_analytic via KSW_inventory; KSWCO
        # has none): without it there is nothing to group, and the card stays empty.
        issues = []
        issue_dom = out_dom + self._datetime_span(start, end) + [('location_dest_id.usage', '!=', 'customer')]
        has_analytic = 'analytic_distribution' in Move._fields
        for row in Move.search_read(issue_dom, ['product_id', 'product_uom_qty', 'analytic_distribution'],
                                    limit=20000) if has_analytic else []:
            dist = row.get('analytic_distribution') or {}
            val = row['product_uom_qty'] * cost.get(row['product_id'][0], 0.0)
            for key, pct in dist.items():
                issues.append((key.split(',')[0], val * (pct or 0) / 100.0))
        by_cc = defaultdict(float)
        for k, v in issues:
            by_cc[k] += v
        accounts = self.env['account.analytic.account'].browse(
            [int(k) for k in by_cc if k.isdigit()]).exists()
        top_cc = sorted(({'id': a.id, 'name': a.display_name, 'value': by_cc[str(a.id)],
                          'domain': issue_dom + [('analytic_distribution', 'in', [a.id])]} for a in accounts),
                        key=lambda r: -r['value'])[:8]

        # Receipts and issues by value across the period: by day, week or month.
        trend_unit, buckets = _trend_buckets(period, today)
        in_dom = [('state', '=', 'done'), ('company_id', '=', company.id), ('product_id.is_storable', '=', True),
                  ('location_dest_id.usage', '=', 'internal'), ('location_id.usage', 'not in', ('internal', 'inventory'))]
        span = self._datetime_span(start, end)

        def worth(dom):
            values = defaultdict(float)
            for day, p, q in Move._read_group(dom + span, ['date:day', 'product_id'], ['product_uom_qty:sum']):
                values[day.date()] += q * unit_cost(p)
            return _sum_by_bucket(buckets, values)
        trend = [{'label': label, 'received': r, 'issued': i}
                 for (label, _a, _b), r, i in zip(buckets, worth(in_dom), worth(out_dom))]

        cur = company.currency_id
        return {
            'currency': {'symbol': cur.symbol, 'position': cur.position, 'digits': cur.decimal_places},
            'value': stock_value, 'items_in_stock': len(in_stock),
            'by_category': sorted(({'id': c.id, 'name': c.display_name, 'value': v} for c, v in value_by_cat.items()
                                   if v), key=lambda r: -r['value'])[:8],
            'by_warehouse': sorted(({'id': w.id, 'name': w.display_name or self.env._('No warehouse'), 'value': v}
                                    for w, v in value_by_wh.items() if v), key=lambda r: -r['value'])[:8],
            'dead': {'count': len(dead), 'value': sum(r['value'] for r in dead), 'rows': dead[:8],
                     'ids': [r['id'] for r in dead]},
            'slow': {'count': len(slow), 'value': sum(r['value'] for r in slow), 'rows': slow[:8],
                     'ids': [r['id'] for r in slow]},
            'out_of_stock': {'count': len(out_of_stock), 'rows': out_of_stock[:8], 'ids': [r['id'] for r in out_of_stock]},
            'running_low': {'count': len(running_low), 'rows': running_low[:8], 'ids': [r['id'] for r in running_low]},
            'below_min': len(below_min), 'below_min_ids': below_min,
            'negative': {'count': len(negative), 'ids': sorted(negative)},
            'turnover': round(turnover, 2), 'dio': round(dio),
            'operations': ops[:10], 'issues_by_cost_centre': top_cc, 'trend': trend, 'trend_unit': trend_unit,
        }
