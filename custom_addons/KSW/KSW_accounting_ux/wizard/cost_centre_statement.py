"""Statement of account for cost centres (analytic accounts).

Like the workshop reports: a prompt first, then the statement as a list
(opening balance, every movement, running balance, with each row opening its
document) and the same as a PDF.

Two menus, one engine:

* **Cost Centre Statement**: one cost centre, picked by name or code;
* **Cost Centres Statement**: several at once, by a **code range** (codes are
  hierarchical, ``1410001`` = T140 under ``141``, so the range compares them as
  text and "141 to 142" covers everything under 141), a **list**, or a whole
  **group** of the tree (a sub-plan, e.g. one branch's vehicles). Shown either
  as one statement per cost centre or as a **summary**, one row each with
  opening, debit, credit and closing.

A journal line can be split across cost centres (``{"12,34": 60, "56": 40}``),
so it counts for a cost centre at the share its distribution gives it: the sum
of the percentages of every key naming the account. A BAS cost centre can exist
in both root plans with the same code (a driver is in *Cost Centres* where BAS
put him in the main column, in *Employee Cost Centre* beside a vehicle); the two
are always read as one cost centre.
"""
from collections import defaultdict

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_PL_TYPES = ('income', 'income_other', 'expense', 'expense_depreciation', 'expense_direct_cost')
_SCOPES = [
    ('all', 'All accounts'),
    ('pl', 'Income and expenses only'),
    ('bs', 'Balance sheet only'),
    ('accounts', 'Chosen accounts'),
]
#: Past this many cost centres a detailed statement is unreadable; the summary is not.
_MAX_DETAILED = 400


class KswCostCentreStatement(models.TransientModel):
    _name = 'ksw.cost.centre.statement'
    _description = 'Cost Centre Statement'
    _transient_max_hours = 12.0

    plan_filter = fields.Selection([
        ('all', 'All cost centres'),
        ('cost_centre', 'Vehicles and departments'),
        ('employee', 'Employees and drivers'),
    ], string='Look In', required=True, default='all')
    allowed_plan_ids = fields.Many2many('account.analytic.plan', compute='_compute_allowed_plans')
    # Every plan under the allowed roots. Not a root_id domain: its search
    # method only takes '=', and Odoo 19 hands it 'in' (pitfall #24).
    allowed_group_ids = fields.Many2many('account.analytic.plan', compute='_compute_allowed_plans')
    selection_mode = fields.Selection([
        ('one', 'One cost centre'),
        ('several', 'Chosen cost centres'),
        ('range', 'A range of codes'),
        ('group', 'A whole group'),
    ], string='Which', required=True, default='one')
    analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Cost Centre',
        domain="[('root_plan_id', 'in', allowed_plan_ids)]",
        help='Type part of the name or the code.')
    analytic_account_ids = fields.Many2many(
        'account.analytic.account', string='Cost Centres',
        domain="[('root_plan_id', 'in', allowed_plan_ids)]")
    code_from = fields.Char(string='From Code')
    code_to = fields.Char(string='To Code',
                          help='Codes are compared as text, so 141 to 142 covers 1410001, 1410999 …')
    group_plan_id = fields.Many2one(
        'account.analytic.plan', string='Group',
        domain="[('id', 'in', allowed_group_ids)]",
        help='Every cost centre in this group of the cost-centre tree, sub-groups included.')
    layout = fields.Selection([
        ('detail', 'A statement per cost centre'),
        ('summary', 'Summary: one row per cost centre'),
    ], string='Show', required=True, default='detail')

    period_mode = fields.Selection([('period', 'A period'), ('all', 'All history')],
                                   string='Records', required=True, default='period')
    date_from = fields.Date(string='From', default=lambda s: fields.Date.context_today(s).replace(month=1, day=1))
    date_to = fields.Date(string='To', default=fields.Date.context_today)
    account_scope = fields.Selection(_SCOPES, string='Accounts', required=True, default='all')
    account_ids = fields.Many2many('account.account', string='Chosen Accounts')
    posted_only = fields.Boolean(string='Posted Entries Only', default=True)

    line_ids = fields.One2many('ksw.cost.centre.statement.line', 'statement_id')
    unit_count = fields.Integer(readonly=True)
    opening_balance = fields.Monetary(readonly=True)
    total_debit = fields.Monetary(readonly=True)
    total_credit = fields.Monetary(readonly=True)
    closing_balance = fields.Monetary(readonly=True)
    currency_id = fields.Many2one('res.currency', default=lambda s: s.env.company.currency_id)

    @api.depends('plan_filter')
    def _compute_allowed_plans(self):
        Plan = self.env['account.analytic.plan']
        roots = Plan.search([('parent_id', '=', False)])
        by_code = {p.x_bas_code: p for p in roots if p.x_bas_code}
        for wiz in self:
            if wiz.plan_filter == 'cost_centre' and 'WREF10' in by_code:
                # Branches and vehicles are top-level plans of their own too.
                wiz.allowed_plan_ids = self.env['ksw.bas.gl.import']._cost_centre_root_plans()
            elif wiz.plan_filter == 'employee' and 'WREF10-EMP' in by_code:
                wiz.allowed_plan_ids = by_code['WREF10-EMP']
            else:
                wiz.allowed_plan_ids = roots
            groups = Plan
            # _origin: in the form the record is new and the ids are NewId placeholders.
            for root in wiz.allowed_plan_ids._origin:
                groups |= Plan.search([('parent_path', '=like', f'{root.id}/%')])
            wiz.allowed_group_ids = groups

    @api.onchange('plan_filter')
    def _onchange_plan_filter(self):
        allowed = self.allowed_plan_ids
        if self.analytic_account_id and self.analytic_account_id.root_plan_id not in allowed:
            self.analytic_account_id = False
        self.analytic_account_ids = self.analytic_account_ids.filtered(lambda a: a.root_plan_id in allowed)
        if self.group_plan_id and self.group_plan_id not in self.allowed_group_ids:
            self.group_plan_id = False

    @api.onchange('period_mode')
    def _onchange_period_mode(self):
        if self.period_mode == 'all':
            self.date_from = self.date_to = False
        elif not self.date_from:
            today = fields.Date.context_today(self)
            self.date_from, self.date_to = today.replace(month=1, day=1), today

    # ------------------------------------------------------------------
    # What is being reported
    # ------------------------------------------------------------------
    def _picked_accounts(self):
        Account = self.env['account.analytic.account']
        allowed = [('root_plan_id', 'in', self.allowed_plan_ids.ids)]
        if self.selection_mode == 'one':
            if not self.analytic_account_id:
                raise UserError(_('Choose the cost centre.'))
            return self.analytic_account_id
        if self.selection_mode == 'several':
            if not self.analytic_account_ids:
                raise UserError(_('Choose the cost centres.'))
            return self.analytic_account_ids
        if self.selection_mode == 'group':
            if not self.group_plan_id:
                raise UserError(_('Choose the group.'))
            return Account.search(allowed + [('plan_id', 'child_of', self.group_plan_id.id)], order='code, name')
        low, high = (self.code_from or '').strip(), (self.code_to or '').strip()
        if not low and not high:
            raise UserError(_('Enter the range: a first code, a last code, or both.'))
        if low and high and low > high:
            raise UserError(_('The first code is after the last code.'))
        accounts = Account.search(allowed + [('code', '!=', False)], order='code, name')
        # "To 142" must include 1429999: compare the code cut to the bound's length.
        return accounts.filtered(lambda a: (not low or a.code >= low)
                                 and (not high or a.code[:len(high)] <= high))

    def _units(self):
        """[(representative account, account ids)] — a BAS code is one cost centre."""
        picked = self._picked_accounts()
        if not picked:
            raise UserError(_('No cost centre matches this selection.'))
        Account = self.env['account.analytic.account']
        codes = [c for c in picked.mapped('x_bas_code') if c]
        twins = defaultdict(lambda: Account)
        if codes:
            for acc in Account.search([('x_bas_code', 'in', codes)]):
                twins[acc.x_bas_code] |= acc
        units, seen = [], set()
        for acc in picked:
            key = acc.x_bas_code or ('id', acc.id)
            if key in seen:
                continue
            seen.add(key)
            ids = (twins[acc.x_bas_code] if acc.x_bas_code else acc) | acc
            units.append((acc, set(ids.ids)))
        return units

    def _account_domain(self):
        if self.account_scope == 'pl':
            return [('account_id.account_type', 'in', _PL_TYPES)]
        if self.account_scope == 'bs':
            return [('account_id.account_type', 'not in', _PL_TYPES)]
        if self.account_scope == 'accounts':
            if not self.account_ids:
                raise UserError(_('Choose the accounts, or show all accounts.'))
            return [('account_id', 'in', self.account_ids.ids)]
        return []

    @staticmethod
    def _shares(line, unit_ids):
        """{unit index: fraction of ``line`` that belongs to it}."""
        out = defaultdict(float)
        for key, pct in (line.analytic_distribution or {}).items():
            ids = {int(i) for i in key.split(',') if i.isdigit()}
            for index, members in unit_ids:
                if ids & members:
                    out[index] += (pct or 0.0) / 100.0
        return out

    # ------------------------------------------------------------------
    def _build(self):
        self.ensure_one()
        if self.period_mode == 'period' and self.date_from and self.date_to and self.date_from > self.date_to:
            raise UserError(_('The start date is after the end date.'))
        units = self._units()
        if self.layout == 'detail' and len(units) > _MAX_DETAILED:
            raise UserError(_(
                '%(n)s cost centres match: too many for a statement each. Show the summary, '
                'or narrow the selection.', n=len(units)))
        unit_ids = list(enumerate(ids for _acc, ids in units))
        every_id = sorted(set().union(*(ids for _acc, ids in units)))
        MoveLine = self.env['account.move.line']
        base = [('analytic_distribution', 'in', every_id),
                ('display_type', 'not in', ('line_section', 'line_note'))]
        base += self._account_domain()
        base.append(('parent_state', '=', 'posted') if self.posted_only else ('parent_state', '!=', 'cancel'))
        start = self.date_from if self.period_mode == 'period' else False
        end = self.date_to if self.period_mode == 'period' else False

        cur = self.currency_id
        opening = defaultdict(float)
        if start:
            for line in MoveLine.search(base + [('date', '<', start)]):
                for i, share in self._shares(line, unit_ids).items():
                    opening[i] += line.balance * share
        domain = list(base)
        if start:
            domain.append(('date', '>=', start))
        if end:
            domain.append(('date', '<=', end))
        moves = defaultdict(list)
        for line in MoveLine.search(domain, order='date, move_name, id'):
            for i, share in self._shares(line, unit_ids).items():
                moves[i].append((line, share))

        vals, seq = [], 0
        totals = defaultdict(float)
        for i, (acc, _ids) in enumerate(units):
            running = opening[i]
            debit_sum = credit_sum = 0.0
            if self.layout == 'detail':
                seq += 1
                vals.append({'statement_id': self.id, 'sequence': seq, 'cost_centre_id': acc.id,
                             'date': start or False, 'is_opening': True,
                             'label': _('Opening balance'), 'running_balance': cur.round(running)})
            for line, share in moves[i]:
                debit, credit = cur.round(line.debit * share), cur.round(line.credit * share)
                running += debit - credit
                debit_sum += debit
                credit_sum += credit
                if self.layout == 'detail':
                    seq += 1
                    vals.append({
                        'statement_id': self.id, 'sequence': seq, 'cost_centre_id': acc.id,
                        'date': line.date, 'move_line_id': line.id, 'move_id': line.move_id.id,
                        'document': line.move_id.name, 'journal_id': line.journal_id.id,
                        'account_id': line.account_id.id, 'partner_id': line.partner_id.id,
                        'label': line.name or line.move_id.ref or '',
                        'share': share * 100.0, 'debit': debit, 'credit': credit,
                        'running_balance': cur.round(running),
                    })
            if self.layout == 'summary':
                if not (opening[i] or debit_sum or credit_sum):
                    continue
                seq += 1
                vals.append({'statement_id': self.id, 'sequence': seq, 'cost_centre_id': acc.id,
                             'is_summary': True, 'opening': cur.round(opening[i]),
                             'debit': debit_sum, 'credit': credit_sum,
                             'running_balance': cur.round(running)})
            totals['opening'] += opening[i]
            totals['debit'] += debit_sum
            totals['credit'] += credit_sum
        self.line_ids.unlink()
        self.env['ksw.cost.centre.statement.line'].create(vals)
        self.write({'unit_count': len(units), 'opening_balance': cur.round(totals['opening']),
                    'total_debit': totals['debit'], 'total_credit': totals['credit'],
                    'closing_balance': cur.round(totals['opening'] + totals['debit'] - totals['credit'])})

    def _title(self):
        period = (_('%(start)s to %(end)s', start=self.date_from or '…', end=self.date_to or '…')
                  if self.period_mode == 'period' else _('all history'))
        if self.selection_mode == 'one':
            what = self.analytic_account_id.display_name
        elif self.selection_mode == 'group':
            what = self.group_plan_id.display_name
        elif self.selection_mode == 'range':
            what = _('codes %(low)s to %(high)s', low=self.code_from or '…', high=self.code_to or '…')
        else:
            what = _('%(n)s cost centres', n=self.unit_count)
        return _('Statement: %(cc)s, %(period)s', cc=what, period=period)

    def action_show(self):
        self._build()
        summary = self.layout == 'summary'
        several = self.unit_count > 1
        view = 'view_cost_centre_statement_line_summary' if summary else (
            'view_cost_centre_statement_line_list_grouped' if several
            else 'view_cost_centre_statement_line_list')
        return {
            'type': 'ir.actions.act_window',
            'name': self._title(),
            'res_model': 'ksw.cost.centre.statement.line',
            'views': [(self.env.ref('KSW_accounting_ux.' + view).id, 'list'), (False, 'pivot')],
            'search_view_id': [self.env.ref('KSW_accounting_ux.view_cost_centre_statement_line_search').id, 'search'],
            'domain': [('statement_id', '=', self.id)],
            'context': {'create': False, 'edit': False, 'delete': False,
                        'group_by': ['cost_centre_id'] if several and not summary else []},
            'target': 'current',
        }

    def action_print(self):
        self._build()
        return self.env.ref('KSW_accounting_ux.action_report_cost_centre_statement').report_action(self)

    def _report_units(self):
        """[(cost centre, lines)] in statement order, for the PDF."""
        groups = defaultdict(lambda: self.env['ksw.cost.centre.statement.line'])
        order = []
        for line in self.line_ids:
            if line.cost_centre_id not in groups:
                order.append(line.cost_centre_id)
            groups[line.cost_centre_id] |= line
        return [(cc, groups[cc]) for cc in order]


class KswCostCentreStatementLine(models.TransientModel):
    _name = 'ksw.cost.centre.statement.line'
    _description = 'Cost Centre Statement Line'
    _order = 'sequence, id'
    _transient_max_hours = 12.0

    statement_id = fields.Many2one('ksw.cost.centre.statement', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer()
    cost_centre_id = fields.Many2one('account.analytic.account', string='Cost Centre')
    is_opening = fields.Boolean()
    is_summary = fields.Boolean()
    date = fields.Date()
    move_line_id = fields.Many2one('account.move.line', string='Journal Item')
    move_id = fields.Many2one('account.move', string='Entry')
    document = fields.Char(string='Document')
    journal_id = fields.Many2one('account.journal', string='Journal')
    account_id = fields.Many2one('account.account', string='Account')
    partner_id = fields.Many2one('res.partner', string='Partner')
    label = fields.Char(string='Description')
    share = fields.Float(string='Share %', digits=(5, 2), aggregator=False,
                         help='The part of the journal line booked to this cost centre.')
    opening = fields.Monetary(string='Opening', aggregator='sum')
    debit = fields.Monetary(aggregator='sum')
    credit = fields.Monetary(aggregator='sum')
    running_balance = fields.Monetary(string='Balance', aggregator=False)
    currency_id = fields.Many2one(related='statement_id.currency_id')

    def action_open_document(self):
        self.ensure_one()
        if not self.move_id:
            return False
        return {'type': 'ir.actions.act_window', 'res_model': 'account.move',
                'res_id': self.move_id.id, 'view_mode': 'form', 'target': 'current'}

    def action_open_cost_centre_statement(self):
        """From a summary row to that cost centre's own statement, same period."""
        self.ensure_one()
        st = self.statement_id
        one = st.copy({'selection_mode': 'one', 'analytic_account_id': self.cost_centre_id.id,
                       'layout': 'detail', 'plan_filter': 'all'})
        return one.action_show()
