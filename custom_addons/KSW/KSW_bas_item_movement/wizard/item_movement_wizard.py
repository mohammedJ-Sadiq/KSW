"""Selection screen for «الحركة التجارية للأصناف».

The SAP way round, like the workshop reports: the prompt asks the period,
branches and items and how to group; Show reads BAS **live** for exactly that
selection, stores the lines for this run and opens them as an interactive
grouped list (pivot and graph alongside).

Cost centres: BAS stores a line's cost centre as a NAME in free text
(``vou10.COST_CENTER`` = vehicle, ``COST_CENTER2`` = driver). The name is
turned into the WREF10 code with BAS's own master, and the code into the
analytic account KSW_bas_gl_import created for it — the same route as
``ksw.bas.gl.import.action_apply_cost_centres``, so a renamed account in
Odoo still matches. A name BAS has but Odoo does not is shown as text,
never guessed.
"""
import re
from collections import defaultdict
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

#: Delivery notes / POS sales: the only document type with item lines (STR10).
FTYPES = ('600',)
#: Past this, the selection is too wide for one screen — and for the HTTP
#: worker's time limit. A month of every item is ~38,000 lines.
MAX_LINES = 80000

_GROUPINGS = [
    ('none', 'Nothing'),
    ('day', 'Day'),
    ('week', 'Week'),
    ('month', 'Month'),
    ('branch', 'Branch'),
    ('warehouse', 'Warehouse'),
    ('vehicle', 'Vehicle Cost Centre'),
    ('driver', 'Driver Cost Centre'),
    ('customer', 'Customer'),
    ('item', 'Item'),
    ('invoice_factor', 'Invoice Multiplier'),
    ('customer_factor', 'Customer Multiplier'),
]
_GROUP_SPECS = {
    'day': 'date:day',
    'week': 'date:week',
    'month': 'date:month',
    'branch': 'branch_id',
    'warehouse': 'warehouse',
    'vehicle': 'vehicle_account_id',
    'driver': 'driver_account_id',
    'customer': 'customer_name',
    'item': 'item_name',
    'invoice_factor': 'invoice_factor',
    'customer_factor': 'customer_factor',
}


def _norm(name):
    return re.sub(r'\s+', ' ', (name or '').replace('\xa0', ' ')).strip().lower()


class KswBasItemMovementWizard(models.TransientModel):
    _name = 'ksw.bas.item.movement.wizard'
    _description = 'BAS Item Movement'
    _transient_max_hours = 12.0

    date_from = fields.Date(
        string='From', required=True,
        default=lambda self: fields.Date.context_today(self).replace(day=1))
    date_to = fields.Date(string='To', required=True, default=fields.Date.context_today)
    branch_ids = fields.Many2many('ksw.bas.branch', string='Branches')
    item_codes = fields.Char(
        string='Item Codes', default='11032',
        help='BAS item codes separated by commas, e.g. 11032 (tanker water). '
             'Leave empty for every item.')
    only_differences = fields.Boolean(
        string='Only Where the Multipliers Differ',
        help='Lines whose invoice multiplier is not the customer\'s current one.')

    layout = fields.Selection([
        ('collapsed', 'Grouped, collapsed (totals per group)'),
        ('expanded', 'Grouped, expanded (every line under its group)'),
        ('flat', 'All lines, no grouping'),
    ], string='Show', required=True, default='collapsed')
    group_by = fields.Selection(_GROUPINGS, string='Group By', required=True, default='driver')
    group_by_2 = fields.Selection(_GROUPINGS, string='Then By', required=True, default='invoice_factor')

    line_ids = fields.One2many('ksw.bas.item.movement.line', 'wizard_id')

    # ------------------------------------------------------------------
    def action_show(self):
        self.ensure_one()
        self._check_inputs()
        self.line_ids.unlink()
        rows, wref = self._fetch_bas()
        self.env['ksw.bas.item.movement.line'].create(self._line_vals(rows, wref))
        group_by = [] if self.layout == 'flat' else [
            _GROUP_SPECS[key] for key in (self.group_by, self.group_by_2) if key != 'none']
        list_view = self.env.ref('KSW_bas_item_movement.view_item_movement_line_list%s' % (
            '_expanded' if self.layout == 'expanded' else ''))
        domain = [('wizard_id', '=', self.id)]
        if self.only_differences:
            domain.append(('factors_differ', '=', True))
        return {
            'type': 'ir.actions.act_window',
            'name': self._title(),
            'res_model': 'ksw.bas.item.movement.line',
            'views': [(list_view.id, 'list'), (False, 'pivot'), (False, 'graph'), (False, 'form')],
            'search_view_id': [self.env.ref(
                'KSW_bas_item_movement.view_item_movement_line_search').id, 'search'],
            'domain': domain,
            'context': {'group_by': group_by, 'create': False, 'edit': False, 'delete': False},
            'target': 'current',
        }

    def _check_inputs(self):
        if self.date_from > self.date_to:
            raise UserError(_('The start date is after the end date.'))
        if self.layout == 'flat':
            self.group_by = self.group_by_2 = 'none'
        elif self.group_by == 'none':
            raise UserError(_('Choose what to group by, or show all lines without grouping.'))

    def _title(self):
        period = '%s → %s' % (self.date_from, self.date_to)
        return _('Item Movement %(period)s', period=period)

    def _item_list(self):
        return [c.strip() for c in (self.item_codes or '').split(',') if c.strip()]

    # ------------------------------------------------------------------
    # BAS
    # ------------------------------------------------------------------
    def _where(self):
        """WHERE clause on STR10 ``s`` and its parameters."""
        clauses = ['s.FTYPE IN (%s)' % ','.join(['%s'] * len(FTYPES)),
                   's.FDATE >= %s', 's.FDATE < %s']
        params = list(FTYPES) + [fields.Date.to_string(self.date_from),
                                 fields.Date.to_string(self.date_to + timedelta(days=1))]
        items = self._item_list()
        if items:
            clauses.append('RTRIM(s.ICODE) IN (%s)' % ','.join(['%s'] * len(items)))
            params += items
        if self.branch_ids:
            codes = self.branch_ids.mapped('code')
            clauses.append('RTRIM(s.CODE2) IN (%s)' % ','.join(['%s'] * len(codes)))
            params += codes
        return ' AND '.join(clauses), params

    def _fetch_bas(self):
        where, params = self._where()
        conn = self.env['ksw.bas.connector']._bas_connect()
        try:
            cur = conn.cursor(as_dict=True)
            cur.execute('SELECT COUNT(*) n FROM STR10 s WHERE ' + where, tuple(params))
            count = cur.fetchone()['n']
            if count > MAX_LINES:
                raise UserError(_(
                    'BAS has %(count)s item lines for this selection, more than %(max)s '
                    'can be shown at once. Choose a shorter period, fewer branches or '
                    'particular items.', count=count, max=MAX_LINES))
            # The cost centres and «رد الفاتورة» sit on the voucher's revenue
            # row (the customer row carries none), one per document: take the
            # row that has a cost centre. A customer row is in cod10 once, but
            # TOP 1 keeps a duplicate from doubling the line.
            cur.execute("""
                SELECT s.FTYPE, s.FTYPE2, RTRIM(s.CODE2) CODE2, s.NUMBER1, s.FDATE,
                       RTRIM(s.ICODE) ICODE, s.IDSCR, s.QUAN, s.IUNIT, s.PRICE,
                       s.AMOUNT, s.TAX_AMOUNT, RTRIM(ISNULL(s.FCODE, '')) FCODE, s.FNAME,
                       c.FACTORE, h.PHRDIS, h.cc1, h.cc2, h.cc3
                FROM STR10 s
                OUTER APPLY (
                    SELECT TOP 1 v.PHRDIS,
                           RTRIM(ISNULL(v.COST_CENTER, '')) cc1,
                           RTRIM(ISNULL(v.COST_CENTER2, '')) cc2,
                           RTRIM(ISNULL(v.COST_CENTER3, '')) cc3
                    FROM vou10 v
                    WHERE v.FTYPE = s.FTYPE AND v.FTYPE2 = s.FTYPE2
                      AND v.CODE2 = s.CODE2 AND v.NUMBER1 = s.NUMBER1
                    ORDER BY CASE WHEN ISNULL(v.COST_CENTER2, '') <> ''
                                    OR ISNULL(v.COST_CENTER, '') <> '' THEN 0 ELSE 1 END,
                             v.SERIAL
                ) h
                OUTER APPLY (
                    SELECT TOP 1 FACTORE FROM cod10 WHERE DCODE1 = s.FCODE
                ) c
                WHERE """ + where, tuple(params))
            rows = cur.fetchall()
            names = {_norm(r['cc1']) for r in rows} | {_norm(r['cc2']) for r in rows}
            names.discard('')
            wref = {}
            if names:
                cur.execute("SELECT RTRIM(CODE) code, NAME FROM WREF10 ORDER BY CODE")
                for r in cur.fetchall():
                    key = _norm(r['NAME'])
                    if key in names:
                        wref.setdefault(key, r['code'])   # lowest code wins, as on the ledger
        finally:
            conn.close()
        return rows, wref

    # ------------------------------------------------------------------
    # Cost centres → analytic accounts
    # ------------------------------------------------------------------
    def _analytic_maps(self):
        """({code: main account}, {code: employee account}) for the BAS plans."""
        Import = self.env['ksw.bas.gl.import']
        Plan = self.env['account.analytic.plan'].sudo().with_context(active_test=False)
        Account = self.env['account.analytic.account'].sudo().with_context(active_test=False)
        maps = []
        for root_code in (Import._COST_CENTRE_ROOT, Import._EMPLOYEE_ROOT):
            root = Plan.search([('x_bas_code', '=', root_code)], limit=1)
            accounts = Account.search([('x_bas_code', '!=', False),
                                       ('root_plan_id', '=', root.id)]) if root else Account
            maps.append({a.x_bas_code: a.id for a in accounts})
        return maps

    def _line_vals(self, rows, wref):
        """Line values for BAS ``rows``; ``wref`` maps a normalised name to its WREF10 code."""
        main, employee = self._analytic_maps()
        branches = self.env['ksw.bas.branch']._for_codes({r['CODE2'] for r in rows})

        def account(name, prefer=None):
            code = wref.get(_norm(name))
            if not code:
                return False
            if prefer is not None and code in prefer:
                return prefer[code]
            return main.get(code, False)

        cache = defaultdict(dict)
        vals_list = []
        for r in rows:
            cc1, cc2 = (r['cc1'] or '').strip(), (r['cc2'] or '').strip()
            if cc1 not in cache['v']:
                cache['v'][cc1] = account(cc1)
            if cc2 not in cache['d']:
                cache['d'][cc2] = account(cc2, prefer=employee)
            cust = round(float(r['FACTORE'] or 0.0), 4)
            inv = round(float(r['PHRDIS'] or 0.0), 4)
            amount = float(r['AMOUNT'] or 0.0)
            tax = float(r['TAX_AMOUNT'] or 0.0)
            branch = branches.get(r['CODE2'])
            number = '%d' % r['NUMBER1'] if r['NUMBER1'] is not None else ''
            vals_list.append({
                'wizard_id': self.id,
                'date': r['FDATE'].date() if r['FDATE'] else False,
                'document': '%s/%s' % (r['CODE2'], number),
                'bas_key': '%s/%s/%s/%s' % (r['FTYPE'], r['FTYPE2'], r['CODE2'], number),
                'branch_id': branch.id if branch else False,
                'warehouse': (r['cc3'] or '').strip() or False,
                'customer_code': r['FCODE'] or False,
                'customer_name': (r['FNAME'] or '').strip() or r['FCODE'] or False,
                'item_code': r['ICODE'],
                'item_name': (r['IDSCR'] or '').strip() or r['ICODE'],
                'quantity': float(r['QUAN'] or 0.0),
                'unit': (r['IUNIT'] or '').strip() or False,
                'price': float(r['PRICE'] or 0.0),
                'amount': amount,
                'tax_amount': tax,
                'total': amount + tax,
                'vehicle_account_id': cache['v'][cc1],
                'vehicle_cost_centre': cc1 or False,
                'driver_account_id': cache['d'][cc2],
                'driver_cost_centre': cc2 or False,
                'customer_factor': cust,
                'invoice_factor': inv,
                'factor_difference': inv - cust,
                'factors_differ': abs(inv - cust) > 0.0001,
                'invoice_factor_missing': not inv,
            })
        return vals_list
