"""The read-only tools Claude uses to answer a question.

Every tool runs in the asking user's environment, never ``sudo()``: the
assistant sees exactly what the user could see by opening the screens
himself, record rules and field groups included. There is no tool that
writes.

Only models the user reaches through a menu are offered. That keeps the
assistant on business documents (loans, leaves, payslips, vehicles...) and
off technical tables a user may technically read (``ir.*``, mail
internals), and it is the same set the user sees in the apps.
"""
import json
import logging
import re

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.fields import Domain

_logger = logging.getLogger(__name__)

MAX_ROWS = 50
DEFAULT_ROWS = 20
MAX_GROUPS = 60
MAX_FIELDS = 60
MAX_MODELS = 15
# Never offered, even to an administrator who has the menus: system
# parameters hold the API keys, and none of these answer a business
# question. Whatever the model reads is sent to the provider.
_TECHNICAL_PREFIX = ('ir.', 'base.', 'bus.', 'res.config', 'base_import.',
                     'mail.', 'auth_', 'iap.', 'fetchmail.', 'ksw.ai.')
_TECHNICAL_MODELS = {'res.users.apikeys', 'res.users.settings',
                     'res.groups', 'res.lang', 'res.users.log'}
# Field names that look like credentials are never read or offered.
_SECRET_RE = re.compile(r'pass|secret|token|api_?key|signature|totp|pin\b',
                        re.I)
READ_FIELDS = 60
READ_SUBTABLES = 6
READ_LINES = 25
# Sub-table columns worth showing, in this order; the rest only fill up.
_LINE_PREFERRED = ('name', 'code', 'total', 'amount', 'number_of_days',
                   'number_of_hours', 'x_ksw_uncollected', 'period_date',
                   'date', 'state')
_SKIP_SUBTABLE_PREFIX = ('ir.', 'mail.', 'bus.', 'rating.')
_SKIP_SUBTABLES = {'details_by_salary_rule_category', 'website_message_ids'}
MAX_MODELS_ALL = 80
# Shown even when they do not match the keywords: filters nearly always
# need them.
_ALWAYS_FIELDS = {'name', 'state', 'employee_id', 'date', 'amount',
                  'department_id', 'active', 'company_id'}
_NOISE_FIELDS = {'create_uid', 'write_uid', 'write_date', '__last_update',
                 'display_name', 'has_message', 'rating_ids'}
_SKIP_TYPES = ('binary', 'html', 'properties', 'properties_definition',
               'json', 'reference', 'many2one_reference')
_OP_WORDS = {
    '=': '=', '!=': '≠', '>': '>', '>=': '≥', '<': '<', '<=': '≤',
    'in': '∈', 'not in': '∉', 'ilike': '~', 'not ilike': '≁',
    'like': '~', '=ilike': '~', 'child_of': 'under', 'parent_of': 'above',
    'any': 'has', 'not any': 'has no',
}

TOOLS = [
    {
        'name': 'find_models',
        'description': (
            'List the Odoo document types (models) this user can open, with '
            'the menu names they appear under. Call this first to find the '
            'model behind a word in the question ("loans", "time off", '
            '"payslips", "vehicles"). Pass a few keywords in English and/or '
            'Arabic; pass none to list everything.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'keywords': {
                    'type': 'array', 'items': {'type': 'string'},
                    'description': 'Words to match against menu and model '
                                   'names. Empty for all.'},
            },
            'required': ['keywords'],
            'additionalProperties': False,
        },
        'strict': True,
    },
    {
        'name': 'get_fields',
        'description': (
            'Describe the fields of a model: technical name, label, type, '
            'related model, and the allowed values of selection fields. '
            'Call this before filtering so the domain uses real field names '
            'and real selection values.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'model': {'type': 'string'},
                'keywords': {
                    'type': 'array', 'items': {'type': 'string'},
                    'description': 'Optional words to narrow the list; '
                                   'empty for all fields.'},
            },
            'required': ['model', 'keywords'],
            'additionalProperties': False,
        },
        'strict': True,
    },
    {
        'name': 'read_record',
        'description': (
            'Read ONE record completely: every field that has a value '
            '(computed amounts included, with their help text) and the lines '
            'of its sub-tables (payslip lines, installments, worked days...). '
            'Use it for the record the user has open ("this", "here", '
            '"هذا", "هنا") and to explain where an amount comes from. '
            'Follow a many2one by reading that record too.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'model': {'type': 'string'},
                'record_id': {'type': 'integer'},
            },
            'required': ['model', 'record_id'],
            'additionalProperties': False,
        },
        'strict': True,
    },
    {
        'name': 'search_records',
        'description': (
            'Find records with an Odoo domain and return them; the user sees '
            'them as a table with the filter and an "Open in Odoo" button, '
            'so do not repeat every row in your answer. Returns the total '
            'count even when only some rows are returned. Domain example: '
            '[["amount", ">", 5000], ["state", "=", "active"]]. Dates as '
            '"YYYY-MM-DD". Dotted paths work: ["employee_id.department_id.'
            'name", "ilike", "sales"].'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'model': {'type': 'string'},
                'domain': {
                    'type': 'string',
                    'description': 'The domain as a JSON array, e.g. '
                                   '[["amount", ">", 5000]]. "[]" for all.'},
                'fields': {
                    'type': 'array', 'items': {'type': 'string'},
                    'description': 'Columns to show, most important first '
                                   '(3 to 8).'},
                'order': {
                    'type': 'string',
                    'description': 'e.g. "amount desc"; empty for default.'},
                'limit': {'type': 'integer',
                          'description': 'Rows to return, at most 50.'},
                'title': {
                    'type': 'string',
                    'description': 'Short heading for the table, in the '
                                   "user's language."},
            },
            'required': ['model', 'domain', 'fields', 'order', 'limit',
                         'title'],
            'additionalProperties': False,
        },
        'strict': True,
    },
    {
        'name': 'summarize_records',
        'description': (
            'Count records and total numeric fields, optionally grouped '
            '(by department, state, month...). Use this for "how many", '
            '"total", "per department" questions instead of fetching rows. '
            'group_by entries are field names, dates may take ":month" or '
            '":year". measures are "field:sum", "field:avg", "field:max" or '
            '"field:min"; the count is always included.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'model': {'type': 'string'},
                'domain': {'type': 'string',
                           'description': 'JSON array domain, "[]" for all.'},
                'group_by': {'type': 'array', 'items': {'type': 'string'},
                             'description': 'At most two; empty for a '
                                            'single total.'},
                'measures': {'type': 'array', 'items': {'type': 'string'}},
                'title': {'type': 'string'},
            },
            'required': ['model', 'domain', 'group_by', 'measures', 'title'],
            'additionalProperties': False,
        },
        'strict': True,
    },
]


class ToolInputError(Exception):
    """Bad tool input: returned to Claude as an error so it can retry."""


class KswAiTools(models.AbstractModel):
    _name = 'ksw.ai.tools'
    _description = 'AI Assistant Tools'

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    @api.model
    def _run_tool(self, name, args):
        """Return ``(result_for_claude, display_block_or_None)``.

        Raises ToolInputError for anything Claude should fix and retry.
        """
        method = {
            'find_models': self._tool_find_models,
            'get_fields': self._tool_get_fields,
            'search_records': self._tool_search_records,
            'read_record': self._tool_read_record,
            'summarize_records': self._tool_summarize_records,
        }.get(name)
        if not method:
            raise ToolInputError('Unknown tool %s' % name)
        try:
            return method(**args)
        except ToolInputError:
            raise
        except AccessError:
            raise ToolInputError(
                'The user has no access to that data. Tell them so; do not '
                'try another way around it.')
        except (ValueError, KeyError, TypeError, UserError,
                ValidationError) as exc:
            raise ToolInputError(str(exc)[:500])

    # ------------------------------------------------------------------
    # Catalog: the models this user reaches through a menu
    # ------------------------------------------------------------------
    @api.model
    def _catalog(self):
        """``{model: {'label': str, 'menus': [paths]}}`` for this user."""
        Menu = self.env['ir.ui.menu']
        menus = Menu.browse(Menu._visible_menu_ids())
        catalog = {}
        for menu in menus:
            action = menu.action
            if not action or action._name != 'ir.actions.act_window':
                continue
            model = action.sudo().res_model
            if not model or model not in self.env \
                    or model.startswith(_TECHNICAL_PREFIX) \
                    or model in _TECHNICAL_MODELS:
                continue
            Model = self.env[model]
            if Model._transient or Model._abstract \
                    or not Model.has_access('read'):
                continue
            entry = catalog.setdefault(model, {
                'label': self.env['ir.model']._get(model).name,
                'menus': []})
            if len(entry['menus']) < 4:
                entry['menus'].append(menu.complete_name)
        return catalog

    @api.model
    def _check_model(self, model):
        if model not in self._catalog():
            raise ToolInputError(
                'Model %r is not available to this user. Use find_models.'
                % model)
        return self.env[model]

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------
    @api.model
    def _tool_find_models(self, keywords):
        words = [w.lower() for w in keywords or [] if w and w.strip()]
        rows = []
        for model, entry in sorted(self._catalog().items()):
            hay = ' '.join([model, entry['label']] + entry['menus']).lower()
            if words and not any(w in hay for w in words):
                continue
            # One compact line per model keeps the token count low (Groq's
            # free tier counts every token of every round).
            rows.append('%s | %s | %s' % (model, entry['label'],
                                          ' ; '.join(entry['menus'][:2])))
        if words and not rows:
            return ({'models': [], 'note': 'No match; call again with '
                     'other keywords, or none to list every model.'}, None)
        limit = MAX_MODELS if words else MAX_MODELS_ALL
        return {'models': rows[:limit],
                'more': max(0, len(rows) - limit)}, None

    @api.model
    def _tool_get_fields(self, model, keywords):
        """One compact line per field: ``name type "Label" [values]``."""
        Model = self._check_model(model)
        words = [w.lower() for w in keywords or [] if w and w.strip()]
        described = Model.fields_get(attributes=[
            'string', 'type', 'relation', 'selection', 'searchable'])
        lines, skipped = [], 0
        for name, f in described.items():
            if f['type'] in _SKIP_TYPES or name.startswith(
                    ('message_', 'activity_', 'website_', 'access_')) \
                    or name in _NOISE_FIELDS:
                continue
            if _SECRET_RE.search(name):
                continue
            text = '%s %s "%s"' % (name, f['type'], f.get('string') or '')
            if words and not any(w in text.lower() for w in words) \
                    and name not in _ALWAYS_FIELDS:
                skipped += 1
                continue
            if f.get('relation'):
                text += ' -> %s' % f['relation']
            if f.get('selection'):
                text += ' [%s]' % ', '.join(
                    '%s=%s' % (k, v) for k, v in f['selection'][:15])
            if not f.get('searchable', True):
                text += ' (not searchable)'
            if len(lines) >= MAX_FIELDS:
                skipped += 1
                continue
            lines.append(text)
        return {'model': model, 'fields': lines,
                'other_fields_not_shown': skipped}, None

    @api.model
    def _tool_read_record(self, model, record_id):
        """Every non-empty readable field of one record, plus up to
        READ_LINES lines of each sub-table, compactly."""
        Model = self._check_model(model)
        rec = Model.browse(int(record_id)).exists()
        if not rec:
            raise ToolInputError('No %s record with id %s (or no access).'
                                 % (model, record_id))
        rec.check_access('read')
        readable = Model.fields_get(attributes=['type', 'string', 'help'])
        values, subtables = {}, {}
        # Amounts first: "why is it 7,114" is the usual question, and the
        # field cap must never cut the figure being asked about.
        order = {'monetary': 0, 'float': 0, 'integer': 1, 'many2one': 2,
                 'selection': 2, 'date': 3, 'datetime': 3, 'boolean': 4}
        for name, desc in sorted(readable.items(),
                                 key=lambda kv: order.get(kv[1]['type'], 5)):
            if desc['type'] in _SKIP_TYPES or name in _NOISE_FIELDS \
                    or _SECRET_RE.search(name) \
                    or name.startswith(('message_', 'activity_', 'website_',
                                        'access_')):
                continue
            field = Model._fields[name]
            try:
                value = rec[name]
            except Exception:  # noqa: BLE001 - one bad compute must not
                continue       # hide the rest of the record
            if desc['type'] in ('one2many', 'many2many'):
                if field.comodel_name.startswith(_SKIP_SUBTABLE_PREFIX) \
                        or name in _SKIP_SUBTABLES:
                    continue
                if value and desc['type'] == 'one2many' \
                        and len(subtables) < READ_SUBTABLES:
                    subtables[name] = self._subtable(desc['string'], value)
                elif value:
                    values[name] = self._format_value(field, value)
                continue
            if value in (False, None, '') or (
                    desc['type'] in ('float', 'monetary', 'integer')
                    and not value):
                continue
            text = self._format_value(field, value)
            if isinstance(text, str) and len(text) > 200:
                text = text[:200] + '…'
            entry = '%s = %s' % (desc['string'], text)
            if desc['type'] == 'many2one':
                entry += ' [%s id %s]' % (field.comodel_name, value.id)
            if desc['type'] in ('float', 'monetary', 'integer') \
                    and desc.get('help'):
                entry += '  (help: %s)' % desc['help'][:160].replace('\n', ' ')
            values[name] = entry
            if len(values) >= READ_FIELDS:
                break
        return {'model': model, 'id': rec.id, 'name': rec.display_name,
                'fields': values, 'subtables': subtables}, None

    @api.model
    def _subtable(self, label, lines):
        """A sub-table as ``{'columns': [...], 'rows': [[...]]}``: name /
        code / numbers / dates / states, at most 6 columns."""
        Line = lines.browse()
        readable = Line.fields_get(attributes=['type'])
        columns = [c for c in _LINE_PREFERRED if c in readable][:6]
        for name, desc in readable.items():
            if len(columns) >= 4:
                break
            field = Line._fields[name]
            if name in columns or name in ('id', 'sequence') \
                    or name in _NOISE_FIELDS or not field.store \
                    or name.startswith(('condition_', 'amount_')):
                continue
            if desc['type'] in ('float', 'monetary', 'integer', 'date'):
                columns.append(name)
        rows = []
        for line in lines[:READ_LINES]:
            row = []
            for name in columns:
                try:
                    row.append(self._format_value(Line._fields[name],
                                                  line[name]))
                except Exception:  # noqa: BLE001
                    row.append('?')
            rows.append(row)
        return {
            'label': label,
            'columns': [Line._fields[c]._description_string(self.env)
                        for c in columns],
            'rows': rows,
            'more_lines': max(0, len(lines) - READ_LINES),
        }

    @api.model
    def _parse_domain(self, Model, domain):
        try:
            raw = json.loads(domain) if isinstance(domain, str) else domain
        except json.JSONDecodeError as exc:
            raise ToolInputError('Domain is not valid JSON: %s' % exc)
        if not isinstance(raw, list):
            raise ToolInputError('Domain must be a JSON array.')
        raw = [tuple(x) if isinstance(x, list) else x for x in raw]
        dom = Domain(raw)
        # Validates every field path against the model now, as the user.
        dom.validate(Model)
        return raw, dom

    @api.model
    def _format_value(self, field, value):
        if value is False or value is None:
            return ''
        if field.type == 'many2one':
            return value.display_name if hasattr(value, 'display_name') \
                else (value[1] if isinstance(value, (list, tuple)) else value)
        if field.type in ('one2many', 'many2many'):
            names = value.mapped('display_name')
            return ', '.join(names[:5]) + (' …' if len(names) > 5 else '')
        if field.type == 'selection':
            return dict(field._description_selection(self.env)).get(
                value, value)
        if field.type in ('date', 'datetime'):
            return fields.Date.to_string(value) if field.type == 'date' \
                else fields.Datetime.to_string(value)
        if field.type == 'boolean':
            return _('Yes') if value else _('No')
        return value

    @api.model
    def _tool_search_records(self, model, domain, fields, order, limit,
                             title):
        Model = self._check_model(model)
        raw, dom = self._parse_domain(Model, domain)
        # fields_get already leaves out what the user's groups may not read.
        readable = Model.fields_get(attributes=['type'])
        # "employee_id.name" as a column means the employee: keep the
        # first step instead of failing the call and costing a round.
        fields = list(dict.fromkeys(
            f.split('.')[0] if '.' in f and f.split('.')[0] in readable
            else f for f in fields))
        unknown = [f for f in fields
                   if f not in readable or _SECRET_RE.search(f)]
        if unknown:
            raise ToolInputError(
                'Unknown or restricted fields on %s: %s. Use get_fields.'
                % (model, ', '.join(unknown)))
        names = [f for f in fields if readable[f]['type'] not in _SKIP_TYPES]
        if 'display_name' not in names:
            names.insert(0, 'display_name')
        limit = max(1, min(int(limit or DEFAULT_ROWS), MAX_ROWS))
        total = Model.search_count(dom)
        records = Model.search(dom, order=order or None, limit=limit)
        rows = [[rec.display_name if n == 'display_name'
                 else self._format_value(Model._fields[n], rec[n])
                 for n in names] for rec in records]
        labels = [Model._fields[n]._description_string(self.env)
                  for n in names]
        # Totals over EVERY matching record, not just the rows shown.
        summable = [n for n in names
                    if Model._fields[n].type in ('float', 'monetary',
                                                 'integer')
                    and Model._fields[n].store
                    and Model._fields[n].aggregator == 'sum']
        sums = {}
        if summable and total:
            [values] = Model._read_group(
                dom, [], ['%s:sum' % n for n in summable])
            sums = {Model._fields[n]._description_string(self.env):
                    round(v or 0.0, 2) for n, v in zip(summable, values)}
        block = {
            'kind': 'table',
            'title': title,
            'model': model,
            'model_label': self.env['ir.model']._get(model).name,
            'domain': raw,
            'filter_text': self._domain_text(Model, raw),
            'columns': labels,
            'rows': rows,
            'ids': records.ids,
            'total': total,
            'sums': sums,
        }
        result = {
            'total_matching': total,
            'returned': len(rows),
            'columns': labels,
            'rows': rows,
            'totals_over_all_matching': block['sums'],
        }
        return result, block

    @api.model
    def _tool_summarize_records(self, model, domain, group_by, measures,
                                title):
        Model = self._check_model(model)
        raw, dom = self._parse_domain(Model, domain)
        group_by = list(group_by or [])[:2]
        for g in group_by:
            fname = g.split(':')[0]
            if fname not in Model._fields:
                raise ToolInputError('Unknown group_by field %s' % fname)
        aggs = ['__count']
        for m in measures or []:
            fname, _sep, func = m.partition(':')
            if fname not in Model._fields or func not in (
                    'sum', 'avg', 'max', 'min'):
                raise ToolInputError('Bad measure %s' % m)
            aggs.append(m)
        groups = Model._read_group(dom, group_by, aggs, limit=MAX_GROUPS,
                                   order=None)
        def label(spec):
            fname, _sep, suffix = spec.partition(':')
            text = Model._fields[fname]._description_string(self.env)
            return '%s (%s)' % (text, suffix) if suffix else text

        labels = [label(g) for g in group_by] + [_('Count')] + [
            label(m) for m in aggs[1:]]
        rows = []
        for group in groups:
            row = []
            for g, value in zip(group_by, group[:len(group_by)]):
                field = Model._fields[g.split(':')[0]]
                if isinstance(value, models.BaseModel):
                    row.append(value.display_name or _('None'))
                elif ':' in g and value:
                    row.append(fields.Date.to_string(value)[:7]
                               if g.endswith(':month')
                               else fields.Date.to_string(value)[:4])
                else:
                    row.append(self._format_value(field, value)
                               or _('None'))
            row += [round(v, 2) if isinstance(v, float) else v
                    for v in group[len(group_by):]]
            rows.append(row)
        block = {
            'kind': 'summary',
            'title': title,
            'model': model,
            'model_label': self.env['ir.model']._get(model).name,
            'domain': raw,
            'group_by': group_by,
            'filter_text': self._domain_text(Model, raw),
            'columns': labels,
            'rows': rows,
            'total': sum(r[len(group_by)] for r in rows),
        }
        return {'columns': labels, 'rows': rows,
                'groups_truncated': len(groups) >= MAX_GROUPS}, block

    # ------------------------------------------------------------------
    # The filter in words, shown under every table
    # ------------------------------------------------------------------
    @api.model
    def _path_label(self, Model, path):
        labels, current = [], Model
        for part in path.split('.'):
            field = current._fields.get(part)
            if field is None:
                labels.append(part)
                break
            labels.append(field._description_string(self.env))
            if field.relational:
                current = self.env[field.comodel_name]
        return ' › '.join(labels)

    @api.model
    def _leaf_text(self, Model, leaf):
        path, op, value = leaf
        label = self._path_label(Model, path)
        field = Model._fields.get(path.split('.')[0]) if '.' not in path \
            else None
        if field is not None and field.type == 'many2one' and (
                isinstance(value, int) or (
                    isinstance(value, (list, tuple)) and value
                    and all(isinstance(v, int) for v in value))):
            ids = [value] if isinstance(value, int) else list(value)
            names = self.env[field.comodel_name].browse(ids).exists() \
                .mapped('display_name')
            value = names[0] if isinstance(leaf[2], int) and names \
                else (names or value)
        if field is not None and field.type == 'selection':
            sel = dict(field._description_selection(self.env))
            value = [sel.get(v, v) for v in value] \
                if isinstance(value, (list, tuple)) else sel.get(value, value)
        if isinstance(value, (list, tuple)):
            value = ', '.join(str(v) for v in value)
        elif value is False:
            value = _('empty')
        elif value is True:
            value = _('yes')
        return '%s %s %s' % (label, _OP_WORDS.get(op, op), value)

    @api.model
    def _domain_text(self, Model, raw):
        if not raw:
            return _('All records')
        stack = list(raw)

        def walk():
            item = stack.pop(0)
            if item == '&':
                return '(%s %s %s)' % (walk(), _('and'), walk())
            if item == '|':
                return '(%s %s %s)' % (walk(), _('or'), walk())
            if item == '!':
                return '%s %s' % (_('not'), walk())
            if isinstance(item, (list, tuple)) and len(item) == 3:
                return self._leaf_text(Model, item)
            return str(item)

        parts = []
        while stack:
            parts.append(walk())
        text = (' %s ' % _('and')).join(parts)
        return text[1:-1] if len(parts) == 1 and text.startswith('(') \
            else text
