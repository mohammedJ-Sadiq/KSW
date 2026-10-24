"""Data for the Workshop overview page.

Modelled on SAP S/4HANA's Maintenance Backlog Overview (backlog by status and
age, rework orders), Oracle Fusion's Maintenance Management infolets (released,
past due, scheduled vs completed, problem assets) and Odoo's Repairs overview
(counts by state, technician workload, top repaired items): the open backlog
and how old it is, what came in and went out in the period, how long a job
takes, which vehicles come back, who did the work and what it cost. Every
figure carries the domain of the requests behind it.

Read as the calling user, so record rules apply; the Reviewer rule reads every
request, which is why the page is for Reviewers (and Managers, who imply it).
"""
from collections import defaultdict
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError
from odoo.addons.KSW_erp_dashboard.models.erp_dashboard import _period, _sum_by_bucket, _trend_buckets

_OPEN = ('new', 'returned', 'in_progress')
# A vehicle back for the same repair type within this many days of completing
# it is a repeat (SAP's "rework order", a fleet workshop's comeback). Same type,
# not any visit: the tankers are in every few days for routine work, and "any
# visit within 30 days" flagged 92% of 2026's jobs. Same type within 14 days: 14%.
_REPEAT_DAYS = 14


class KswWorkshopDashboard(models.AbstractModel):
    _name = 'ksw.workshop.dashboard'
    _description = 'Workshop Overview'

    @api.model
    def get_workshop_data(self, period='month'):
        # A Technician's rule only reaches in-progress and his own jobs, so his
        # figures would be silently partial: the page is the Reviewer's.
        if not self.env.su and not self.env.user.has_group('KSW_workshop.group_workshop_reviewer'):
            raise AccessError(self.env._('The workshop overview is for workshop reviewers and managers.'))
        today = fields.Date.context_today(self)
        now = fields.Datetime.now()
        start, end, pstart, pend = _period(period, today)
        span = self.env['ksw.erp.dashboard']._datetime_span
        Req = self.env['ksw.workshop.request']

        def count(domain):
            return Req.search_count(domain)

        received = span(start, end, 'create_date')
        completed = [('state', '=', 'completed')] + span(start, end, 'completion_date')
        prev_completed = [('state', '=', 'completed')] + span(pstart, pend, 'completion_date')

        def avg_days(domain):
            return Req._read_group(domain, [], ['duration_days:avg'])[0][0] or 0.0

        # Backlog: what is open now, and how long it has been waiting.
        open_dom = [('state', 'in', _OPEN)]
        new_dom = [('state', '=', 'new')]
        stale_new = new_dom + [('create_date', '<', now - timedelta(days=2))]
        in_progress = [('state', '=', 'in_progress')]
        oldest = Req.search(in_progress, order='create_date asc', limit=1)
        age_buckets = []
        for key, label, low, high in (('0_2', self.env._('0–2 days'), 0, 2), ('3_7', self.env._('3–7 days'), 3, 7),
                                      ('8_30', self.env._('8–30 days'), 8, 30),
                                      ('30p', self.env._('Over 30 days'), 31, None)):
            dom = open_dom + [('create_date', '<=', now - timedelta(days=low))]
            if high is not None:
                dom.append(('create_date', '>', now - timedelta(days=high + 1)))
            age_buckets.append({'key': key, 'label': label, 'count': count(dom), 'domain': dom})
        by_state = [{'key': s, 'label': dict(Req._fields['state']._description_selection(self.env))[s],
                     'count': count([('state', '=', s)]), 'domain': [('state', '=', s)]} for s in _OPEN]

        # In and out across the period: by day, week or month.
        trend_unit, buckets = _trend_buckets(period, today)

        def by_day(domain, field):
            return {d.date(): n for d, n in Req._read_group(domain, [f'{field}:day'], ['__count'])}
        trend = [{'label': label, 'received': r, 'completed': c} for (label, _a, _b), r, c in zip(
            buckets, _sum_by_bucket(buckets, by_day(received, 'create_date')),
            _sum_by_bucket(buckets, by_day(completed, 'completion_date')))]

        # Repeat repairs: a completed job whose vehicle had a completed job of the
        # same repair type at most _REPEAT_DAYS before this one was raised.
        done = Req.search_read(completed + [('vehicle_id', '!=', False), ('request_type_ids', '!=', False)],
                               ['vehicle_id', 'create_date', 'request_type_ids'])
        earlier = defaultdict(list)
        if done:
            first = min(r['create_date'] for r in done)
            for r in Req.search_read([('state', '=', 'completed'),
                                      ('vehicle_id', 'in', list({r['vehicle_id'][0] for r in done})),
                                      ('completion_date', '>=', first - timedelta(days=_REPEAT_DAYS))],
                                     ['vehicle_id', 'completion_date', 'request_type_ids']):
                earlier[r['vehicle_id'][0]].append((r['id'], r['completion_date'], set(r['request_type_ids'])))
        repeat_ids, repeat_by_vehicle = [], defaultdict(list)
        for r in done:
            vid = r['vehicle_id'][0]
            types = set(r['request_type_ids'])
            if any(pid != r['id'] and types & ptypes
                   and r['create_date'] - timedelta(days=_REPEAT_DAYS) <= when <= r['create_date']
                   for pid, when, ptypes in earlier[vid]):
                repeat_ids.append(r['id'])
                repeat_by_vehicle[r['vehicle_id']].append(r['id'])
        repeat_rows = sorted(({'name': v[1], 'count': len(ids), 'domain': [('id', 'in', ids)]}
                              for v, ids in repeat_by_vehicle.items()), key=lambda r: -r['count'])[:8]

        # Who and what, in the period.
        def top(domain, group, limit=8):
            return Req._read_group(domain + [(group, '!=', False)], [group], ['__count'],
                                   order='__count desc', limit=limit)
        vehicles = [{'name': v.display_name, 'count': n, 'domain': received + [('vehicle_id', '=', v.id)]}
                    for v, n in top(received, 'vehicle_id')]
        types = [{'name': t.display_name, 'count': n, 'domain': received + [('request_type_ids', 'in', t.id)]}
                 for t, n in top(received, 'request_type_ids')]
        clients = [{'name': c.display_name, 'count': n, 'domain': received + [('client_id', '=', c.id),
                                                                              ('is_cash_customer', '=', False)]}
                   for c, n in top(received + [('is_cash_customer', '=', False)], 'client_id', 6)]
        cash = received + [('is_cash_customer', '=', True)]
        if count(cash):
            clients.append({'name': self.env._('Cash customers'), 'count': count(cash), 'domain': cash})

        done_by = dict(top(completed, 'technician_label', 20))
        busy = dict(top(in_progress, 'technician_label', 20))
        technicians = sorted(({'name': name, 'completed': done_by.get(name, 0), 'open': busy.get(name, 0),
                               'domain': [('technician_label', '=', name), '|', ('state', '=', 'in_progress'),
                                          '&', '&'] + completed}
                              for name in set(done_by) | set(busy)),
                             key=lambda r: -(r['completed'] + r['open']))[:8]

        cost = dict(zip(('parts', 'other', 'labor', 'total'), Req._read_group(
            completed, [], ['part_lines_cost:sum', 'parts_cost:sum', 'labor_cost:sum', 'total_cost:sum'])[0]))
        costed = count(completed + [('total_cost', '>', 0)])
        cost_vehicles = [{'name': v.display_name, 'amount': amt, 'domain': completed + [('vehicle_id', '=', v.id)]}
                         for v, amt in Req._read_group(completed + [('vehicle_id', '!=', False), ('total_cost', '>', 0)],
                                                       ['vehicle_id'], ['total_cost:sum'],
                                                       order='total_cost:sum desc', limit=5)]

        done_count = count(completed)
        cur = self.env.company.currency_id
        return {
            'currency': {'symbol': cur.symbol, 'position': cur.position, 'digits': cur.decimal_places},
            'period': {'start': str(start), 'end': str(end)},
            'open': {'count': count(open_dom), 'domain': open_dom, 'returned': count([('state', '=', 'returned')])},
            'new': {'count': count(new_dom), 'domain': new_dom, 'stale': count(stale_new), 'stale_domain': stale_new},
            'in_progress': {'count': count(in_progress), 'domain': in_progress,
                            'oldest_days': (now - oldest.create_date).days if oldest else 0},
            'received': {'count': count(received), 'domain': received},
            'completed': {'count': done_count, 'domain': completed, 'previous': count(prev_completed)},
            'turnaround': {'days': round(avg_days(completed), 1), 'previous': round(avg_days(prev_completed), 1)},
            'repeat': {'count': len(repeat_ids), 'domain': [('id', 'in', repeat_ids)], 'days': _REPEAT_DAYS,
                       'rate': round(len(repeat_ids) * 100.0 / done_count) if done_count else 0,
                       'rows': repeat_rows},
            'age': age_buckets, 'by_state': by_state,
            'trend': trend, 'trend_unit': trend_unit,
            'vehicles': vehicles, 'types': types, 'clients': clients, 'technicians': technicians,
            'cost': {**{k: v or 0.0 for k, v in cost.items()}, 'costed': costed,
                     'per_job': (cost['total'] or 0.0) / costed if costed else 0.0, 'vehicles': cost_vehicles},
        }
