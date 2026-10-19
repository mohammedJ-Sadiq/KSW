"""Workshop reports: by date, by vehicle, by repair type, by spare part.

The SAP way round — a selection screen, then the list. The wizard asks the
questions (which period or all history, which vehicle / type / part, how to
group, collapsed or expanded or flat) and Show turns the answers into the
domain and group-bys of an interactive list (rows open the repair, pivot and
graph alongside), so nobody has to build filters by hand. Print PDF renders
the same selection as a page to hand someone.

Nothing is stored. Every run reads the requests (and the issued part lines)
with the caller's own rights, so a report can never show a job its reader
could not open — and the Reporting menu is already limited to the two roles
that read every request (Reviewer, Manager), which is what keeps the figures
complete rather than a silently partial aggregate.
"""
from collections import defaultdict
from datetime import datetime, time, timedelta

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.misc import format_date

# A printed report of 7,000 oil changes helps nobody, and wkhtmltopdf would
# take minutes over it. Past this the summary tables are still complete; only
# the itemised rows stop, and the report says so.
DETAIL_LIMIT = 2000
# Free text is cut on the page, never in the data.
TEXT_LIMIT = 160

_TIME_GROUPS = ('day', 'week', 'month', 'year')


class KswWorkshopReportWizard(models.TransientModel):
    _name = 'ksw.workshop.report.wizard'
    _description = 'Workshop Report'

    report_type = fields.Selection([
        ('date', 'By Date'),
        ('vehicle', 'By Vehicle'),
        ('repair_type', 'By Repair Type'),
        ('spare_part', 'By Spare Part'),
    ], string='Report', required=True, default='date')

    period_mode = fields.Selection([
        ('period', 'A period'),
        ('all', 'All history'),
    ], string='Records', required=True, default='period')
    date_from = fields.Date(string='From')
    date_to = fields.Date(string='To', default=fields.Date.context_today)
    date_basis = fields.Selection([
        ('request', 'Request Date'),
        ('completion', 'Completion Date'),
    ], string='Date Basis', required=True, default='request',
        help="Which date of the request the period applies to. Spare parts always "
             "use the date the part was issued.")
    state_scope = fields.Selection([
        ('completed', 'Completed only'),
        ('active', 'All except rejected'),
        ('all', 'All'),
    ], string='Requests', required=True, default='completed')

    vehicle_id = fields.Many2one('ksw.fleet.vehicle', string='Vehicle')
    vehicle_type = fields.Selection(
        lambda self: self.env['ksw.workshop.request']._fields['vehicle_type'].selection,
        string='Vehicle Type')
    client_id = fields.Many2one('res.partner', string='Client')
    request_type_ids = fields.Many2many(
        'ksw.workshop.request.type', string='Repair Types',
        help="Leave empty for every type, including requests nobody classified.")
    part_id = fields.Many2one(
        'ksw.workshop.part', string='Spare Part',
        help="Leave empty for a ranking of every part issued in the period.")
    part_source = fields.Selection([
        ('issued', 'Issued on repairs (with quantities)'),
        ('notes', 'Mentioned in the repair notes'),
    ], string='Look In', required=True, default='issued',
        help="Parts listed item by item on a repair carry a quantity and a cost. Most of "
             "the history predates that: what was fitted is written in the repair notes.")
    text_search = fields.Char(
        string='Also Search Repair Notes For',
        help="Most of the history predates itemised parts: what was fitted is written "
             "in the repair notes. Requests whose notes contain this text are listed "
             "separately — they are mentions, not quantities.")

    _GROUPINGS = [
        ('none', 'Nothing'),
        ('day', 'Day'),
        ('week', 'Week'),
        ('month', 'Month'),
        ('year', 'Year'),
        ('vehicle', 'Vehicle'),
        ('repair_type', 'Repair Type'),
        ('part', 'Spare Part'),
        ('technician', 'Technician'),
        ('vehicle_type', 'Vehicle Type'),
        ('client', 'Client'),
    ]
    layout = fields.Selection([
        ('collapsed', 'Grouped, collapsed (totals per group)'),
        ('expanded', 'Grouped, expanded (every record under its group)'),
        ('flat', 'All records, no grouping'),
    ], string='Show', required=True, default='collapsed')
    group_by = fields.Selection(_GROUPINGS, string='Group By', required=True, default='day')
    group_by_2 = fields.Selection(_GROUPINGS, string='Then By', required=True, default='none')
    show_details = fields.Boolean(
        string='List Each Request', default=True,
        help="Untick for the summary tables only — useful over long periods.")

    # ------------------------------------------------------------------
    # Defaults and form behaviour
    # ------------------------------------------------------------------
    def _type_defaults(self, report_type):
        """Period, grouping and scope that suit each report.

        By Date opens on this month; the three histories open on all of it.
        A vehicle's history includes the job it is in for today.
        """
        return {
            'period_mode': 'period' if report_type == 'date' else 'all',
            'date_from': fields.Date.context_today(self).replace(day=1) if report_type == 'date' else False,
            'group_by': {'date': 'day', 'vehicle': 'vehicle', 'repair_type': 'repair_type',
                         'spare_part': 'part'}[report_type],
            'group_by_2': {'date': 'none', 'vehicle': 'year'}.get(report_type, 'month'),
            'layout': 'expanded' if report_type == 'date' else 'collapsed',
            'state_scope': 'active' if report_type == 'vehicle' else 'completed',
        }

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        context = self.env.context
        for field, value in self._type_defaults(res.get('report_type') or 'date').items():
            if field in fields_list and not context.get('default_' + field):
                res[field] = value
        if res.get('vehicle_id') and res.get('group_by') == 'vehicle':
            res.update(self._one_vehicle_layout())
        return res

    @staticmethod
    def _one_vehicle_layout():
        # One vehicle: its years, every visit listed.
        return {'group_by': 'year', 'group_by_2': 'none', 'layout': 'expanded'}

    @api.onchange('vehicle_id')
    def _onchange_vehicle_id(self):
        for wizard in self:
            if wizard.report_type != 'vehicle':
                continue
            if wizard.vehicle_id and wizard.group_by == 'vehicle':
                wizard.update(wizard._one_vehicle_layout())
            elif not wizard.vehicle_id and wizard.group_by != 'vehicle':
                wizard.update({'group_by': 'vehicle', 'group_by_2': 'year', 'layout': 'collapsed'})

    @api.model_create_multi
    def create(self, vals_list):
        # default_get never sees a report_type passed in vals (it only fills
        # the fields that are missing), so a vehicle history created without
        # the menu's context would otherwise get By Date's "this month".
        context = self.env.context
        for vals in vals_list:
            report_type = vals.get('report_type') or context.get('default_report_type') or 'date'
            for field, value in self._type_defaults(report_type).items():
                if not context.get('default_' + field):
                    vals.setdefault(field, value)
        return super().create(vals_list)

    @api.onchange('report_type')
    def _onchange_report_type(self):
        for wizard in self:
            wizard.update(wizard._type_defaults(wizard.report_type))

    @api.onchange('period_mode')
    def _onchange_period_mode(self):
        for wizard in self:
            if wizard.period_mode == 'all':
                wizard.date_from = wizard.date_to = False
            elif not wizard.date_from and not wizard.date_to:
                today = fields.Date.context_today(wizard)
                wizard.date_from, wizard.date_to = today.replace(day=1), today

    @api.onchange('part_id')
    def _onchange_part_id(self):
        for wizard in self:
            if wizard.part_id:
                wizard._apply_part_choice()

    @api.onchange('part_source')
    def _onchange_part_source(self):
        # Notes are free text on the repair: there is no part to group them by.
        for wizard in self:
            if wizard.part_source == 'notes':
                if wizard.group_by == 'part':
                    wizard.group_by = 'year'
                if wizard.group_by_2 == 'part':
                    wizard.group_by_2 = 'none'

    def _apply_part_choice(self):
        """Prefill the notes search, and look in the notes when nothing was itemised.

        A part chosen with no issued lines would otherwise open an empty
        list — while its history sits in the notes.
        """
        self.text_search = self._part_search_text()
        has_lines = self.env['ksw.workshop.part.line'].search_count(self._part_line_part_domain(), limit=1)
        self.part_source = 'issued' if has_lines else 'notes'
        self._onchange_part_source()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_show(self):
        """Open the interactive list the answers describe."""
        self.ensure_one()
        self._check_inputs()
        on_lines = self._shows_part_lines()
        domain = self._part_line_domain() if on_lines else self._request_domain()
        if self.report_type == 'spare_part' and not on_lines:
            domain += self._notes_domain()
        group_by = [] if self.layout == 'flat' else [
            self._group_spec(key, on_lines) for key in (self.group_by, self.group_by_2) if key != 'none']
        prefix = 'view_ksw_workshop_part_line_list_report' if on_lines else 'view_ksw_workshop_request_list_report'
        list_view = self.env.ref('KSW_workshop.%s%s' % (prefix, '_expanded' if self.layout == 'expanded' else ''))
        search_view = self.env.ref('KSW_workshop.view_ksw_workshop_part_line_search' if on_lines
                                   else 'KSW_workshop.view_ksw_workshop_request_search')
        views = [(list_view.id, 'list'), (False, 'pivot'), (False, 'graph')]
        if not on_lines:
            views.append((False, 'form'))
        return {
            'type': 'ir.actions.act_window',
            'name': self._action_title(),
            'res_model': 'ksw.workshop.part.line' if on_lines else 'ksw.workshop.request',
            'views': views,
            'search_view_id': [search_view.id, 'search'],
            'domain': domain,
            'context': {'group_by': group_by, 'create': False,
                        # Grouping by a many2many opens a group for every type a
                        # matching repair carries; keep only the chosen ones.
                        'ksw_workshop_type_group_ids': self._chosen_types().ids},
            'target': 'current',
        }

    def action_view(self):
        if self.report_type == 'vehicle' and not self.vehicle_id:
            raise UserError(_('Choose the vehicle whose history you want to print.'))
        self._check_inputs()
        return self.env.ref('KSW_workshop.action_report_workshop_html').report_action(self)

    def action_print(self):
        if self.report_type == 'vehicle' and not self.vehicle_id:
            raise UserError(_('Choose the vehicle whose history you want to print.'))
        self._check_inputs()
        return self.env.ref('KSW_workshop.action_report_workshop_pdf').report_action(self)

    def _check_inputs(self):
        for wizard in self:
            if wizard.period_mode == 'all':
                wizard.write({'date_from': False, 'date_to': False})
            elif not wizard.date_from and not wizard.date_to:
                raise UserError(_('Enter the period: a start date, an end date, or both.'))
            if wizard.date_from and wizard.date_to and wizard.date_from > wizard.date_to:
                raise UserError(_('The start date is after the end date.'))
            if wizard.layout == 'flat':
                # The printed report reads the same answer: no grouping.
                wizard.group_by = 'none'
            elif wizard.group_by == 'none':
                raise UserError(_('Choose what to group by, or show all records without grouping.'))
            # Fail on the form, not as an empty or crashing list.
            on_lines = wizard._shows_part_lines()
            for key in (wizard.group_by, wizard.group_by_2):
                if key != 'none':
                    wizard._group_spec(key, on_lines)

    def _shows_part_lines(self):
        return self.report_type == 'spare_part' and self.part_source == 'issued'

    def _date_field(self):
        return 'completion_date' if self.date_basis == 'completion' else 'create_date'

    def _part_group_field(self):
        return 'part_id'

    def _group_spec(self, key, on_lines):
        """A Group By answer as a list group-by, for requests or for part lines."""
        if key in _TIME_GROUPS:
            return '%s:%s' % ('issue_date' if on_lines else self._date_field(), key)
        if on_lines:
            spec = {'vehicle': 'vehicle_id', 'part': self._part_group_field(),
                    'technician': 'technician_id', 'client': 'client_id'}.get(key)
        else:
            spec = {'vehicle': 'vehicle_id', 'repair_type': 'request_type_ids',
                    'technician': 'technician_label', 'vehicle_type': 'vehicle_type',
                    'client': 'client_id'}.get(key)
        if not spec:
            label = self._selection_label(self._name, 'group_by', key)
            if on_lines:
                raise UserError(_('Issued spare parts cannot be grouped by %s.', label))
            raise UserError(_('Repairs cannot be grouped by %s. Choose Spare Part only when '
                              'looking at issued parts.', label))
        return spec

    def _notes_domain(self):
        text = (self.text_search or '').strip()
        if not text:
            raise UserError(_('Enter the text to look for in the repair notes, e.g. فلتر زيت.'))
        return ['|', '|', '|', ('repairs_parts', 'ilike', text), ('work_statement', 'ilike', text),
                ('description', 'ilike', text), ('part_line_ids.description', 'ilike', text)]

    def _action_title(self):
        """The list's title states the selection, since it is not a search facet."""
        if self.period_mode == 'all':
            period = _('all history')
        else:
            period = '%s → %s' % (self._fmt_date(self.date_from) or '…', self._fmt_date(self.date_to) or '…')
        if self.report_type == 'spare_part' and not self._shows_part_lines():
            subject = _('Repair notes mentioning “%s”', self.text_search)
        else:
            subject = self._title()
        return '%s · %s' % (subject, period)

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------
    def _tz(self):
        return pytz.timezone(self.env.context.get('tz') or self.env.user.tz or 'Asia/Riyadh')

    def _utc_bounds(self):
        """date_from 00:00 and date_to 24:00 local, as naive UTC datetimes.

        Request dates are datetimes stored in UTC; filtering them on a bare
        date would put a 01:30 Riyadh job on the previous day.
        """
        tz = self._tz()
        start = end = None
        if self.date_from:
            start = tz.localize(datetime.combine(self.date_from, time.min)).astimezone(pytz.utc).replace(tzinfo=None)
        if self.date_to:
            end = tz.localize(datetime.combine(self.date_to + timedelta(days=1), time.min)).astimezone(pytz.utc).replace(tzinfo=None)
        return start, end

    def _local_date(self, value):
        if not value:
            return False
        if isinstance(value, datetime):
            return pytz.utc.localize(value).astimezone(self._tz()).date()
        return value

    def _fmt_date(self, value):
        return format_date(self.env, value) if value else ''

    def _period_key(self, day):
        """(sort key, label) of the time bucket a date falls in."""
        if not day:
            return (('0',), _('No date'))
        if self.group_by == 'day':
            return ((day.isoformat(),), format_date(self.env, day, date_format='EEE d MMM yyyy'))
        if self.group_by == 'week':
            year, week, _weekday = day.isocalendar()
            return (('%d-%02d' % (year, week),), _('Week %(week)s, %(year)s', week=week, year=year))
        if self.group_by == 'month':
            return ((day.strftime('%Y-%m'),), format_date(self.env, day, date_format='MMMM yyyy'))
        return ((str(day.year),), str(day.year))

    def _selection_label(self, model, field, value):
        return dict(self.env[model]._fields[field]._description_selection(self.env)).get(value, value or '')

    @staticmethod
    def _cut(text):
        text = ' '.join((text or '').split())
        return text if len(text) <= TEXT_LIMIT else text[:TEXT_LIMIT - 1] + '…'

    @staticmethod
    def _num(value):
        return '{:,.2f}'.format(value or 0.0)

    def _vehicle_label(self, request):
        if request.vehicle_id:
            return request.vehicle_id.display_name
        if request.is_cash_customer:
            return request.x_cash_vehicle_number or request.x_cash_customer_name or _('Cash customer')
        return _('No vehicle')

    def _type_labels(self, request):
        return request.request_type_ids.mapped('name') or [_('Unclassified')]

    # ------------------------------------------------------------------
    # Request selection
    # ------------------------------------------------------------------
    def _state_domain(self, field='state'):
        if self.state_scope == 'completed':
            return [(field, '=', 'completed')]
        if self.state_scope == 'active':
            return [(field, '!=', 'rejected')]
        return []

    def _request_domain(self):
        date_field = 'completion_date' if self.date_basis == 'completion' else 'create_date'
        start, end = self._utc_bounds()
        domain = self._state_domain()
        if start:
            domain.append((date_field, '>=', start))
        if end:
            domain.append((date_field, '<', end))
        if self.vehicle_id:
            domain.append(('vehicle_id', '=', self.vehicle_id.id))
        if self.vehicle_type:
            domain.append(('vehicle_type', '=', self.vehicle_type))
        if self.client_id:
            domain.append(('client_id', '=', self.client_id.id))
        if self.report_type == 'repair_type' and self.request_type_ids:
            domain.append(('request_type_ids', 'in', self.request_type_ids.ids))
        return domain

    def _request_date(self, request):
        value = request.completion_date if self.date_basis == 'completion' else request.create_date
        return self._local_date(value)

    def _chosen_types(self):
        """The repair types the user narrowed the report to, if any."""
        if self.report_type == 'repair_type':
            return self.request_type_ids
        return self.env['ksw.workshop.request.type']

    def _shown_types(self, request):
        """A request's types as groups: only the chosen ones, when types were chosen.

        A Brakes repair that also changed the oil must not open an Oil & Filters
        group on a report the user narrowed to Brakes.
        """
        chosen = self._chosen_types()
        return request.request_type_ids & chosen if chosen else request.request_type_ids

    def _group_keys(self, request):
        """Every (sort key, label) a request is counted under for group_by.

        A request carries several repair types at once (oil AND brakes), so
        under Repair Type it is counted in each — the group rows then add up
        to more than the request total, and the report says so.
        """
        group_by = self.group_by
        if group_by == 'none':
            return [((0,), _('All requests'))]
        if group_by in _TIME_GROUPS:
            return [self._period_key(self._request_date(request))]
        if group_by == 'vehicle':
            label = self._vehicle_label(request)
            return [((label,), label)]
        if group_by == 'repair_type':
            if not request.request_type_ids:
                return [((1, 0), _('Unclassified'))]
            return [((0, rtype.sequence, rtype.id), rtype.name) for rtype in self._shown_types(request)]
        if group_by == 'technician':
            label = request.technician_label or _('Not recorded')
            return [((label,), label)]
        if group_by == 'client':
            label = request.client_id.display_name or request.x_cash_customer_name or _('Not set')
            return [((label,), label)]
        if group_by == 'part':
            return [((0,), _('All requests'))]
        label = self._selection_label('ksw.workshop.request', 'vehicle_type', request.vehicle_type) or _('Not set')
        return [((label,), label)]

    @staticmethod
    def _bucket(records, keys_of):
        """{(sort key, label): {'requests': recordset}}, in one pass.

        Ids are collected and browsed once per bucket: unioning recordsets
        row by row is quadratic, and "all history" is 17,000 requests.
        """
        ids = defaultdict(list)
        for record in records:
            for key in keys_of(record):
                ids[key].append(record.id)
        return {key: {'requests': records.browse(found)} for key, found in ids.items()}

    def _group_order(self, buckets):
        """Time buckets read in date order, everything else busiest first."""
        if self.group_by in _TIME_GROUPS or self.group_by in ('none', 'repair_type'):
            return sorted(buckets.items(), key=lambda item: item[0][0])
        return sorted(buckets.items(), key=lambda item: (-len(item[1]['requests']), item[0][1]))

    # ------------------------------------------------------------------
    # One request as a printed row
    # ------------------------------------------------------------------
    def _request_row(self, request):
        work = []
        if request.labor_line_ids:
            work += [line.description for line in request.labor_line_ids]
        elif request.work_statement:
            work.append(request.work_statement)
        parts = [
            '%s × %g' % (line.description or self._line_part_name(line), line.quantity)
            for line in request.part_line_ids
        ]
        if not parts and request.repairs_parts:
            parts.append(request.repairs_parts)
        return {
            'date': self._fmt_date(self._request_date(request)),
            'name': request.name,
            'vehicle': self._vehicle_label(request),
            'types': ', '.join(self._type_labels(request)),
            'request': self._cut(request.description),
            'work': self._cut(' · '.join(work)),
            'parts': self._cut(' · '.join(parts)),
            'technician': request.technician_label or '',
            'state': self._selection_label('ksw.workshop.request', 'state', request.state),
            'odometer': request.odometer_reading or '',
            'cost': request.total_cost,
        }

    @staticmethod
    def _column_flags(rows):
        """Drop a free-text column nothing on this page fills.

        Most of the history has no work statement, itemised parts or
        technician; three empty columns would squeeze the request text into
        a sliver for nothing.
        """
        return {
            'show_work': any(r['work'] for r in rows),
            'show_parts': any(r['parts'] for r in rows),
            'show_technician': any(r['technician'] for r in rows),
        }

    def _summarise(self, requests):
        completed = requests.filtered(lambda r: r.state == 'completed')
        durations = [r.duration_days for r in completed if r.completion_date]
        return {
            'count': len(requests),
            'completed': len(completed),
            'vehicles': len(requests.vehicle_id),
            'cost': sum(requests.mapped('total_cost')),
            'avg_days': (sum(durations) / len(durations)) if durations else 0.0,
        }

    # ------------------------------------------------------------------
    # Report data — one entry point, read by the QWeb template
    # ------------------------------------------------------------------
    def get_report_data(self):
        self.ensure_one()
        if self.report_type == 'spare_part':
            data = self._spare_part_data()
        else:
            data = self._request_report_data()
        data['filters'] = self._filter_lines()
        data['report_label'] = self._selection_label(self._name, 'report_type', self.report_type)
        data['printed_on'] = format_date(self.env, fields.Date.context_today(self))
        data['printed_by'] = self.env.user.name
        return data

    def _filter_lines(self):
        lines = []
        if self.date_from or self.date_to:
            period = '%s → %s' % (self._fmt_date(self.date_from) or _('the beginning'),
                                  self._fmt_date(self.date_to) or _('today'))
        else:
            period = _('All history')
        lines.append((_('Period'), period))
        if self.report_type != 'spare_part':
            lines.append((_('Date Basis'), self._selection_label(self._name, 'date_basis', self.date_basis)))
        lines.append((_('Requests'), self._selection_label(self._name, 'state_scope', self.state_scope)))
        if self.vehicle_id:
            lines.append((_('Vehicle'), self.vehicle_id.display_name))
        if self.vehicle_type:
            lines.append((_('Vehicle Type'), self._selection_label(self._name, 'vehicle_type', self.vehicle_type)))
        if self.client_id:
            lines.append((_('Client'), self.client_id.display_name))
        if self.report_type == 'repair_type':
            lines.append((_('Repair Types'), ', '.join(self.request_type_ids.mapped('name')) or _('All, including unclassified')))
        if self.report_type == 'spare_part':
            lines.append((_('Spare Part'), self._part_label() or _('All parts')))
            if self.text_search:
                lines.append((_('Repair notes searched for'), self.text_search))
        lines.append((_('Group By'), self._selection_label(self._name, 'group_by', self.group_by)))
        return lines

    def _request_report_data(self):
        Request = self.env['ksw.workshop.request']
        requests = Request.search(self._request_domain(), order='create_date asc, id asc')
        # Oldest first is how a history is read; the request date is the
        # stable order even when the basis is completion.
        if self.date_basis == 'completion':
            requests = requests.sorted(lambda r: (r.completion_date or r.create_date, r.id))
        totals = self._summarise(requests)
        show_cost = bool(totals['cost'])

        kpis = [
            (_('Requests'), totals['count']),
            (_('Completed'), totals['completed']),
            (_('Vehicles'), totals['vehicles']),
            (_('Avg. Days in Workshop'), '%.1f' % totals['avg_days']),
        ]
        if self.report_type == 'vehicle':
            del kpis[2]
        if show_cost:
            kpis.append((_('Total Cost'), self._num(totals['cost'])))

        tables = []
        if self.report_type == 'vehicle':
            kpis, tables = self._vehicle_extras(requests, kpis, tables)
        if self.report_type == 'repair_type' or (self.report_type == 'vehicle' and self.group_by != 'repair_type'):
            tables.append(self._by_type_table(requests))
        if self.report_type == 'repair_type' and self.group_by != 'vehicle':
            tables.append(self._top_vehicles_table(requests))

        ordered = self._group_order(self._bucket(requests, self._group_keys))
        group_table = self._group_table(ordered, totals, show_cost)
        if self.group_by != 'none':
            tables.insert(0, group_table)

        groups, shown = [], 0
        if self.show_details:
            for (_key, label), bucket in ordered:
                if shown >= DETAIL_LIMIT:
                    break
                rows = [self._request_row(r) for r in bucket['requests'][:DETAIL_LIMIT - shown]]
                shown += len(rows)
                groups.append({'label': label, 'count': len(bucket['requests']), 'rows': rows})
        listed = sum(len(b['requests']) for _k, b in ordered)
        return {
            **self._column_flags([row for g in groups for row in g['rows']]),
            'title': self._title(),
            'kpis': kpis,
            'tables': tables,
            'groups': groups,
            'grouped': self.group_by != 'none',
            'show_vehicle': self.report_type != 'vehicle',
            'show_cost': show_cost,
            'truncated': listed - shown if self.show_details and shown < listed else 0,
            'multi_count_note': self.group_by == 'repair_type' and listed > totals['count'],
            'empty': not requests,
        }

    def _title(self):
        if self.report_type == 'vehicle':
            if not self.vehicle_id:
                return _('Vehicle History')
            return _('Workshop History — %s', self.vehicle_id.display_name)
        if self.report_type == 'repair_type':
            names = self.request_type_ids.mapped('name')
            return _('Repairs by Type — %s', ', '.join(names)) if names else _('Repairs by Type')
        if self.report_type == 'spare_part':
            label = self._part_label()
            return _('Spare Part — %s', label) if label else _('Spare Parts Issued')
        return _('Workshop Repairs by Date')

    def _group_table(self, ordered, totals, show_cost):
        # One vehicle per row (or one vehicle in all) makes a vehicle count noise.
        count_vehicles = self.group_by != 'vehicle' and self.report_type != 'vehicle'

        def _row(label, s):
            row = [label, s['count'], s['completed']]
            if count_vehicles:
                row.append(s['vehicles'])
            row.append('%.1f' % s['avg_days'])
            if show_cost:
                row.append(self._num(s['cost']))
            return row

        header = [self._selection_label(self._name, 'group_by', self.group_by), _('Requests'), _('Completed')]
        if count_vehicles:
            header.append(_('Vehicles'))
        header.append(_('Avg. Days'))
        if show_cost:
            header.append(_('Cost'))
        rows = [_row(label, self._summarise(bucket['requests'])) for (_key, label), bucket in ordered]
        total = _row(_('Total'), totals)
        return {'title': _('Summary by %s', header[0]), 'header': header, 'rows': rows, 'total': total,
                'numeric_from': 1}

    def _by_type_table(self, requests):
        counts = defaultdict(list)
        order = {}
        for request in requests:
            types = self._shown_types(request)
            if not types:
                counts[_('Unclassified')].append(request.id)
                order[_('Unclassified')] = (1, 0)
            for rtype in types:
                counts[rtype.name].append(request.id)
                order[rtype.name] = (0, rtype.sequence)
        rows = []
        for name in sorted(counts, key=lambda n: order[n]):
            recs = requests.browse(counts[name])
            dates = [d for d in (self._request_date(r) for r in recs) if d]
            rows.append([name, len(recs), len(recs.vehicle_id),
                         self._fmt_date(min(dates)) if dates else '',
                         self._fmt_date(max(dates)) if dates else ''])
        return {'title': _('By Repair Type'),
                'header': [_('Repair Type'), _('Requests'), _('Vehicles'), _('First'), _('Last')],
                'rows': rows, 'total': None, 'numeric_from': 1}

    def _top_vehicles_table(self, requests, limit=15):
        by_vehicle = self._bucket(requests, lambda r: [(None, self._vehicle_label(r))])
        ranked = sorted(((label, b['requests']) for (_k, label), b in by_vehicle.items()),
                        key=lambda item: (-len(item[1]), item[0]))[:limit]
        rows = []
        for label, recs in ranked:
            dates = [d for d in (self._request_date(r) for r in recs) if d]
            rows.append([label, len(recs), self._fmt_date(max(dates)) if dates else ''])
        return {'title': _('Most Frequent Vehicles (top %s)', limit),
                'header': [_('Vehicle'), _('Requests'), _('Last')],
                'rows': rows, 'total': None, 'numeric_from': 1}

    def _vehicle_extras(self, requests, kpis, tables):
        vehicle = self.vehicle_id
        dates = sorted(d for d in (self._request_date(r) for r in requests) if d)
        odometers = requests.filtered('odometer_reading').sorted(lambda r: r.create_date)
        if dates:
            kpis.append((_('First Visit'), self._fmt_date(dates[0])))
            kpis.append((_('Last Visit'), self._fmt_date(dates[-1])))
        if len(dates) > 1:
            gap = (dates[-1] - dates[0]).days / (len(dates) - 1)
            kpis.append((_('Avg. Days Between Visits'), '%.0f' % gap))
        if odometers:
            kpis.append((_('Last Odometer'), '{:,}'.format(odometers[-1].odometer_reading)))
        details = [
            (_('Fleet No.'), vehicle.name),
            (_('Vehicle Type'), self._selection_label('ksw.fleet.vehicle', 'vehicle_type', vehicle.vehicle_type)),
            (_('Client'), vehicle.client_id.display_name or ''),
            (_('Plate Number'), vehicle.plate_number or ''),
            (_('Brand / Model / Year'), ' / '.join(str(v) for v in (vehicle.brand, vehicle.model, vehicle.year) if v)),
            (_('Default Driver'), vehicle.driver_id.name or vehicle.driver_name or ''),
        ]
        tables.append({'title': _('Vehicle'), 'header': None,
                       'rows': [[label, value] for label, value in details if value],
                       'total': None, 'numeric_from': 99, 'key_value': True})
        return kpis, tables

    # ------------------------------------------------------------------
    # Spare parts — the hooks KSW_workshop_stock overrides for stock items
    # ------------------------------------------------------------------
    def _part_line_part_domain(self):
        return [('part_id', '=', self.part_id.id)] if self.part_id else [('part_id', '!=', False)]

    def _line_part(self, line):
        """The item a line issued, as (key record, code, name)."""
        return line.part_id, line.part_id.name or '', line.part_id.description or ''

    def _line_part_name(self, line):
        _rec, code, name = self._line_part(line)
        return name or code

    def _part_label(self):
        if not self.part_id:
            return ''
        return ' — '.join(v for v in (self.part_id.name, self.part_id.description) if v)

    def _part_search_text(self):
        return self.part_id.description or self.part_id.name or ''

    def _part_reference_kpis(self):
        """Prices known from before the workshop moved into Odoo."""
        part = self.part_id
        if not part or not part.x_bas_qty:
            return []
        return [
            (_('BAS Last Price'), '%s (%s)' % (self._num(part.x_bas_last_price), self._fmt_date(part.x_bas_last_date))),
            (_('BAS Average Price'), self._num(part.x_bas_avg_price)),
            (_('BAS Quantity Bought'), '%g' % part.x_bas_qty),
        ]

    def _part_line_domain(self):
        domain = self._part_line_part_domain() + self._state_domain()
        if self.date_from:
            domain.append(('issue_date', '>=', self.date_from))
        if self.date_to:
            domain.append(('issue_date', '<=', self.date_to))
        if self.vehicle_id:
            domain.append(('vehicle_id', '=', self.vehicle_id.id))
        if self.vehicle_type:
            domain.append(('request_id.vehicle_type', '=', self.vehicle_type))
        if self.client_id:
            domain.append(('client_id', '=', self.client_id.id))
        return domain

    def _line_group_key(self, line):
        group_by = self.group_by
        request = line.request_id
        if group_by == 'none':
            return ((0,), _('All issues'))
        if group_by in _TIME_GROUPS:
            return self._period_key(line.issue_date)
        if group_by == 'repair_type':
            rtype = request.request_type_ids[:1]
            return ((0, rtype.sequence, rtype.id), rtype.name) if rtype else ((1, 0), _('Unclassified'))
        if group_by == 'technician':
            label = request.technician_label or _('Not recorded')
            return ((label,), label)
        if group_by == 'vehicle_type':
            label = self._selection_label('ksw.workshop.request', 'vehicle_type', request.vehicle_type) or _('Not set')
            return ((label,), label)
        if group_by == 'part':
            _rec, code, name = self._line_part(line)
            label = ' — '.join(v for v in (code, name) if v) or _('Not set')
            return ((label,), label)
        if group_by == 'client':
            label = line.client_id.display_name or _('Not set')
            return ((label,), label)
        label = self._vehicle_label(request)
        return ((label,), label)

    def _spare_part_data(self):
        Line = self.env['ksw.workshop.part.line']
        lines = Line.search(self._part_line_domain(), order='issue_date asc, id asc')
        qty = sum(lines.mapped('quantity'))
        value = sum(lines.mapped('subtotal'))
        kpis = [
            (_('Quantity Issued'), '%g' % qty),
            (_('Value Issued'), self._num(value)),
            (_('Repairs'), len(lines.request_id)),
            (_('Vehicles'), len(lines.vehicle_id)),
        ]
        if self.part_id:
            if qty:
                kpis.append((_('Avg. Unit Cost'), self._num(value / qty)))
            if lines:
                kpis.append((_('Last Issued'), self._fmt_date(lines[-1].issue_date)))
            kpis += self._part_reference_kpis()

        tables = []
        if not self.part_id and lines:
            tables.append(self._part_ranking_table(lines))

        buckets = {key: b['requests'] for key, b in
                   self._bucket(lines, lambda line: [self._line_group_key(line)]).items()}
        if self.group_by in _TIME_GROUPS or self.group_by in ('none', 'repair_type'):
            ordered = sorted(buckets.items(), key=lambda item: item[0][0])
        else:
            ordered = sorted(buckets.items(), key=lambda item: (-sum(item[1].mapped('quantity')), item[0][1]))
        if self.group_by != 'none' and lines:
            rows = [[label, '%g' % sum(recs.mapped('quantity')), self._num(sum(recs.mapped('subtotal'))),
                     len(recs.request_id), len(recs.vehicle_id)]
                    for (_k, label), recs in ordered]
            tables.insert(0, {
                'title': _('Summary by %s', self._selection_label(self._name, 'group_by', self.group_by)),
                'header': [self._selection_label(self._name, 'group_by', self.group_by),
                           _('Quantity'), _('Value'), _('Repairs'), _('Vehicles')],
                'rows': rows,
                'total': [_('Total'), '%g' % qty, self._num(value), len(lines.request_id), len(lines.vehicle_id)],
                'numeric_from': 1,
            })
        if self.part_id and self.group_by != 'vehicle' and lines:
            tables.append(self._part_by_vehicle_table(lines))

        part_groups, shown = [], 0
        if self.show_details:
            for (_key, label), recs in ordered:
                if shown >= DETAIL_LIMIT:
                    break
                rows = []
                for line in recs[:DETAIL_LIMIT - shown]:
                    _rec, code, name = self._line_part(line)
                    rows.append({
                        'date': self._fmt_date(line.issue_date),
                        'name': line.request_id.name,
                        'vehicle': self._vehicle_label(line.request_id),
                        'part': ' — '.join(v for v in (code, line.description or name) if v),
                        'qty': '%g' % line.quantity,
                        'unit_cost': self._num(line.unit_cost),
                        'subtotal': self._num(line.subtotal),
                        'technician': line.request_id.technician_label or '',
                        'state': self._selection_label('ksw.workshop.request', 'state', line.request_id.state),
                    })
                shown += len(rows)
                part_groups.append({'label': label, 'count': len(recs), 'rows': rows})

        mentions = self._text_mentions()
        return {
            **self._column_flags(mentions['rows'] if mentions else []),
            'title': self._title(),
            'kpis': kpis,
            'tables': tables,
            'groups': [],
            'part_groups': part_groups,
            'grouped': self.group_by != 'none',
            'show_part': not self.part_id,
            'truncated': len(lines) - shown if self.show_details and shown < len(lines) else 0,
            'mentions': mentions,
            'empty': not lines and not (mentions and mentions['count']),
            'no_lines': not lines,
        }

    def _part_ranking_table(self, lines, limit=50):
        stats = {}
        for line in lines:
            rec, code, name = self._line_part(line)
            entry = stats.setdefault(rec, {'code': code, 'name': name, 'qty': 0.0, 'value': 0.0,
                                           'requests': set(), 'vehicles': set(), 'last': line.issue_date})
            entry['qty'] += line.quantity
            entry['value'] += line.subtotal
            entry['requests'].add(line.request_id.id)
            if line.vehicle_id:
                entry['vehicles'].add(line.vehicle_id.id)
            entry['last'] = max(entry['last'], line.issue_date)
        ranked = sorted(stats.values(), key=lambda e: (-e['value'], -e['qty'], e['code']))
        rows = [[e['code'], e['name'], '%g' % e['qty'], self._num(e['value']),
                 self._num(e['value'] / e['qty']) if e['qty'] else '', len(e['requests']),
                 len(e['vehicles']), self._fmt_date(e['last'])] for e in ranked[:limit]]
        title = _('Parts Issued (top %s by value)', limit) if len(ranked) > limit else _('Parts Issued')
        return {'title': title,
                'header': [_('Item No.'), _('Description'), _('Quantity'), _('Value'), _('Avg. Unit Cost'),
                           _('Repairs'), _('Vehicles'), _('Last Issued')],
                'rows': rows, 'total': None, 'numeric_from': 2}

    def _part_by_vehicle_table(self, lines, limit=20):
        by_vehicle = {label: b['requests'] for (_k, label), b in
                      self._bucket(lines, lambda line: [(None, self._vehicle_label(line.request_id))]).items()}
        ranked = sorted(by_vehicle.items(), key=lambda item: (-sum(item[1].mapped('quantity')), item[0]))[:limit]
        rows = [[label, '%g' % sum(recs.mapped('quantity')), self._num(sum(recs.mapped('subtotal'))),
                 self._fmt_date(max(recs.mapped('issue_date')))] for label, recs in ranked]
        return {'title': _('By Vehicle (top %s)', limit),
                'header': [_('Vehicle'), _('Quantity'), _('Value'), _('Last Issued')],
                'rows': rows, 'total': None, 'numeric_from': 1}

    def _text_mentions(self):
        """Requests whose repair notes mention text_search — the pre-Odoo history.

        A separate section, never added to the quantities above: "تم تغيير
        فلتر الزيت" says a part was fitted, not how many, and summing mentions
        into an issued quantity would print a figure nobody recorded.
        """
        text = (self.text_search or '').strip()
        if not text:
            return None
        domain = [d for d in self._request_domain() if d[0] != 'request_type_ids']
        domain += ['|', '|', ('repairs_parts', 'ilike', text), ('work_statement', 'ilike', text),
                   ('description', 'ilike', text)]
        requests = self.env['ksw.workshop.request'].search(domain, order='create_date asc, id asc')
        ordered = self._group_order(self._bucket(requests, self._group_keys))
        rows = [[label, len(bucket['requests']), len(bucket['requests'].vehicle_id)]
                for (_k, label), bucket in ordered]
        details = [self._request_row(r) for r in requests[:DETAIL_LIMIT]] if self.show_details else []
        return {
            'text': text,
            'count': len(requests),
            'vehicles': len(requests.vehicle_id),
            'table': {'title': _('Mentions by %s', self._selection_label(self._name, 'group_by', self.group_by)),
                      'header': [self._selection_label(self._name, 'group_by', self.group_by),
                                 _('Requests'), _('Vehicles')],
                      'rows': rows, 'total': [_('Total'), len(requests), len(requests.vehicle_id)],
                      'numeric_from': 1} if self.group_by != 'none' and requests else None,
            'rows': details,
            'truncated': max(len(requests) - DETAIL_LIMIT, 0) if self.show_details else 0,
        }
