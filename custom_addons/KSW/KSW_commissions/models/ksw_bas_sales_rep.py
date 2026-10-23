"""BAS sales rep → employee mapping.

BAS assigns every customer a sales rep (COD10.SELLER) and a collection rep
(COD10.SELLER2) as a numeric code. The only human label BAS keeps for a code
is the free-text VOU10.SELLERNAME, which is a nickname ("Abu Sadeq",
"Faisal"), a role ("مندوب الكامب") or a house account ("Company collector"),
and which BAS users rename at will ("Abu Sadeq" became "Abu Sadeq-K"). So
matching on that text broke silently every time a name changed.

This model keys the mapping on the CODE. It is filled from the synced BAS
customers on every "Pull from BAS" — the user only ever picks the employee
once per rep, and a rename in BAS changes the label here, never the link.
"""
import re

from odoo import api, fields, models

# BAS appends/prepends a "-K" branch marker to some rep names.
_BRANCH_MARKER = re.compile(r'(^\s*K\s*-\s*)|(\s*-\s*K\s*$)', re.IGNORECASE)
# Codes the commission workbook never pays: the defaulted accounts held by
# "Sales manager" (120309*, المتعثر) and the projects-department customers.
# NOT "Company collector": that is Mohamed Fares, who has a bonus scheme
# (March 2026 workbook, row 31) — flagging it was a mistake corrected in
# 19.0.4.47.0.
_HOUSE_NAME = re.compile(r'sales\s+manager|عملاء\s+قسم', re.IGNORECASE)


def _normalise(name):
    name = _BRANCH_MARKER.sub('', name or '')
    return ' '.join(re.findall(r'\w+', name.lower()))


class KswBasSalesRep(models.Model):
    _name = 'ksw.bas.sales.rep'
    _description = 'BAS Sales Rep Mapping'
    _order = 'state_order, customer_count desc, code'
    _rec_name = 'bas_name'

    code = fields.Char('BAS Rep Code', required=True, readonly=True,
                       index=True)
    bas_name = fields.Char(
        'Name in BAS', readonly=True,
        help='The label BAS uses for this rep code (VOU10.SELLERNAME). '
             'Refreshed on every pull; the employee link does not depend '
             'on it.')
    employee_id = fields.Many2one(
        'hr.employee', string='Employee',
        help='The employee credited with the sales and collections of '
             'every customer BAS assigns to this rep code.')
    is_house = fields.Boolean(
        'No Commission',
        help='This code is a company/house account, not a person (e.g. '
             '"Company collector"). Its customers earn nobody commission '
             'and are not reported as missing a rep.')
    state = fields.Selection([
        ('todo', 'Needs an Employee'),
        ('mapped', 'Mapped'),
        ('house', 'No Commission'),
    ], compute='_compute_state', store=True)
    state_order = fields.Integer(compute='_compute_state', store=True)
    sales_customer_count = fields.Integer(
        'Customers (Sales)', readonly=True)
    collection_customer_count = fields.Integer(
        'Customers (Collection)', readonly=True)
    customer_count = fields.Integer('Customers', readonly=True)

    _code_unique = models.Constraint(
        'UNIQUE(code)', 'Each BAS rep code can only be mapped once.')

    @api.depends('employee_id', 'is_house')
    def _compute_state(self):
        for rec in self:
            if rec.is_house:
                rec.state, rec.state_order = 'house', 2
            elif rec.employee_id:
                rec.state, rec.state_order = 'mapped', 1
            else:
                rec.state, rec.state_order = 'todo', 0

    def write(self, vals):
        res = super().write(vals)
        if {'employee_id', 'is_house'} & set(vals):
            self._recompute_customers()
        return res

    def _recompute_customers(self):
        codes = self.mapped('code')
        customers = self.env['ksw.bas.customer'].sudo().search([
            '|', ('seller_code', 'in', codes),
            ('collector_code', 'in', codes)])
        if customers:  # an empty recordset would recompute every customer
            customers._recompute_effective_reps()

    @api.model
    def _sync_from_bas_customers(self):
        """Create a row for every rep code BAS uses, refresh its label and
        customer counts, and suggest an employee for new rows when the
        name identifies exactly one. Never overwrites a choice a user made.
        """
        Rep = self.sudo()
        self.env.cr.execute("""
            SELECT code, MAX(name), SUM(s), SUM(c) FROM (
                SELECT seller_code AS code, seller_name AS name,
                       1 AS s, 0 AS c
                  FROM ksw_bas_customer
                 WHERE COALESCE(seller_code, '') != ''
                UNION ALL
                SELECT collector_code, collector_name, 0, 1
                  FROM ksw_bas_customer
                 WHERE COALESCE(collector_code, '') != ''
                UNION ALL
                -- Codes stamped on documents: a rep who has since lost all
                -- his customers still has past months to be credited for,
                -- and documents carry a name where COD10's map has none.
                SELECT DISTINCT seller_code, NULLIF(TRIM(seller_name), ''),
                       0, 0
                  FROM ksw_bas_ledger_line
                 WHERE COALESCE(seller_code, '') != ''
            ) t GROUP BY code
        """)
        rows = self.env.cr.fetchall()
        existing = {r.code: r for r in Rep.search([])}
        for code, name, n_sales, n_coll in rows:
            vals = {
                'bas_name': name or '',
                'sales_customer_count': n_sales,
                'collection_customer_count': n_coll,
                'customer_count': max(n_sales, n_coll),
            }
            rep = existing.get(code)
            if rep:
                changed = {k: v for k, v in vals.items() if rep[k] != v}
                if changed:
                    # super().write: a label refresh must not trigger a
                    # customer recompute.
                    super(KswBasSalesRep, rep).write(changed)
                continue
            vals['code'] = code
            if _HOUSE_NAME.search(name or ''):
                vals['is_house'] = True
            else:
                employee = self._suggest_employee(name)
                if employee:
                    vals['employee_id'] = employee.id
            Rep.create(vals)

    @api.model
    def _suggest_employee(self, bas_name):
        """The single active employee this BAS name points to, or empty.

        Tried in order: the commission alias (x_commission_import_name)
        and then the employee name, each compared with the BAS "-K" branch
        marker stripped, and finally every word of a two-word-or-longer
        BAS name appearing in exactly one employee name. A one-word
        nickname ("Faisal") is never guessed, and ambiguity returns
        nothing — a wrong guess would pay the wrong person.
        """
        Employee = self.env['hr.employee'].sudo()
        key = _normalise(bas_name)
        if not key:
            return Employee.browse()
        employees = Employee.search([('active', '=', True)])
        for field in ('x_commission_import_name', 'name'):
            hits = employees.filtered(
                lambda e, f=field: _normalise(e[f]) == key)
            if len(hits) == 1:
                return hits
        words = key.split()
        if len(words) < 2:
            return Employee.browse()
        hits = employees.filtered(
            lambda e: all(w in _normalise(e.name).split() for w in words))
        return hits if len(hits) == 1 else Employee.browse()

    @api.model
    def _employee_by_code(self):
        """{code: hr.employee} for every mapped rep."""
        return {
            r.code: r.employee_id
            for r in self.sudo().search([('employee_id', '!=', False),
                                         ('is_house', '=', False)])
        }

    @api.model
    def _house_codes(self):
        return set(self.sudo().search([('is_house', '=', True)]).mapped('code'))
