"""Sales & collection figures for one month, read from the BAS customer
ledger (``ksw.bas.ledger.line``) the way the accountant's commission
workbook builds them.

Reconciled 2026-10-21 against «عمولة بيع وتحصيل شهر مارس 2026»:

* **Sales** = month-end invoices (VOU10 FTYPE 002) debiting a customer:
  408 of 408 customers matched to the riyal.
* **Collections** = receipts (018 cash, 015 bank) crediting a customer:
  203 of 206 matched.
* **Who** = the rep BAS stamped on each document (SELLER), not the rep the
  customer has today — customers are reassigned between months.
* **Collection target** = the customer's balance at the end of the previous
  month, minus the invoices of the last ``term / 30`` months (not yet due).
  169 of 191 matched; the rest were typed by hand in the workbook.
* Everything is reported net of VAT (the workbook divides by 1.15).

Every one of those choices is a setting (Settings > KSW Commissions >
Sales & Collection from BAS), read here through :meth:`_config`, so a rule
the business changes is a setting, not a code change. Nothing here writes;
the sheet's Pull from BAS and the line target compute both read from it,
so they can never disagree.
"""
from collections import defaultdict
from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models

_P = 'ksw_commissions.sc_'

DEFAULTS = {
    'vat_rate': 15.0,
    'sales_doc_types': '002',
    'sales_return_doc_types': '',
    'collection_doc_types': '018,015',
    'ledger_prefixes': '120301,120303,120306,120309',
    'attribution': 'document',
    'target_owner': 'last_receipt',
    'target_method': 'due_balance',
    'term_source': 'invdays',
    'default_term_days': 30,
}


def _split(value):
    return [v.strip() for v in (value or '').split(',') if v.strip()]


class KswSalesCommissionBas(models.AbstractModel):
    _name = 'ksw.sales.commission.bas'
    _description = 'Sales & Collection figures from the BAS ledger'

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    @api.model
    def _config(self):
        get = self.env['ir.config_parameter'].sudo().get_param
        cfg = {}
        for key, default in DEFAULTS.items():
            raw = get(_P + key)
            if raw in (None, False, ''):
                cfg[key] = default
            elif isinstance(default, float):
                cfg[key] = float(raw)
            elif isinstance(default, int):
                cfg[key] = int(float(raw))
            else:
                cfg[key] = raw
        for key in ('sales_doc_types', 'sales_return_doc_types',
                    'collection_doc_types', 'ledger_prefixes'):
            cfg[key] = _split(cfg[key])
        if cfg['ledger_prefixes'] == ['*']:
            cfg['ledger_prefixes'] = []
        cfg['vat_divisor'] = 1.0 + (cfg['vat_rate'] or 0.0) / 100.0
        return cfg

    @staticmethod
    def _in_scope(account, cfg):
        prefixes = cfg['ledger_prefixes']
        return bool(account) and (
            not prefixes or any(account.startswith(p) for p in prefixes))

    # ------------------------------------------------------------------
    # Who
    # ------------------------------------------------------------------
    @api.model
    def _resolver(self, cfg, year):
        """Return ``resolve(customer, kind, doc_code) -> (code, employee,
        blocked)``.

        Priority: a manual rep on the customer's contact (the Salesperson's employee /
        x_collection_rep_id) > the rep code (on the document, or on the
        customer when attribution is 'customer') mapped through BAS Sales
        Reps. A No Commission code resolves to no employee.

        **The rep's role decides what he can be credited with**, as in the
        workbook (Salesman / Collector / Salesman & Collector, from the
        salesperson profile of that year). A receipt stamped with a
        salesman's code — he carried the cheque in — belongs to the
        customer's collector. If that one cannot take it either, nobody is
        credited and ``blocked`` is True. Sales are never moved: a
        collector's sales are shown on his line, and his role keeps them
        out of his commission. An employee
        without a profile takes anything.
        """
        Rep = self.env['ksw.bas.sales.rep']
        by_code = Rep._employee_by_code()
        house = Rep._house_codes()
        Employee = self.env['hr.employee']
        roles = {
            p.employee_id.id: p.role
            for p in self.env['ksw.salesperson.profile'].sudo().search([
                ('year', '=', year), ('active', '=', True)])
        }
        allowed = {'sales': ('sales', 'both'),
                   'collection': ('collect', 'both')}

        def takes(employee, kind):
            # Only collections are restricted: the workbook still SHOWS a
            # collector's sales (it just pays him no sales commission, which
            # the line's role already decides), but never credits a
            # salesman with a collection.
            if kind == 'sales':
                return True
            role = roles.get(employee.id)
            return not role or role in allowed[kind]

        def own_code(customer, kind):
            if not customer:
                return ''
            return (customer.seller_code if kind == 'sales'
                    else customer.collector_code) or ''

        def by(code):
            if code in house:
                return Employee
            return by_code.get(code, Employee)

        def resolve(customer, kind, doc_code):
            if customer and customer.partner_id:
                override = (customer.partner_id._ksw_sales_rep_employee()
                            if kind == 'sales'
                            else customer.partner_id.x_collection_rep_id)
                if override:
                    return doc_code or own_code(customer, kind), override, False
            if cfg['attribution'] == 'customer' or not doc_code:
                doc_code = own_code(customer, kind)
            code = doc_code or ''
            employee = by(code)
            if not employee or takes(employee, kind):
                return code, employee, False
            # Not his role: the customer's own rep for this kind.
            fallback = own_code(customer, kind)
            if fallback and fallback != code:
                employee = by(fallback)
                if not employee or takes(employee, kind):
                    return fallback, employee, False
                code = fallback
            return code, Employee, True

        return resolve

    # ------------------------------------------------------------------
    # The month
    # ------------------------------------------------------------------
    @api.model
    def _period_bounds(self, period):
        start = fields.Date.to_date(period).replace(day=1)
        return start, start + relativedelta(months=1)

    @api.model
    def _period_figures(self, period):
        """Everything one month needs, per customer and rep.

        Returns ``{'sales': [...], 'collections': [...], 'targets': [...],
        'cfg': cfg}``; each row is a dict with ``account``, ``customer``
        (ksw.bas.customer or empty), ``code``, ``rep_name``, ``employee``,
        ``gross``, ``net`` (and for targets ``balance``, ``not_due``,
        ``term_days``).
        """
        self = self.sudo()
        cfg = self._config()
        start, end = self._period_bounds(period)
        Customer = self.env['ksw.bas.customer']
        customers = {c.bas_code: c for c in Customer.search([])}
        resolve = self._resolver(cfg, start.year)
        labels = self._rep_labels()
        div = cfg['vat_divisor']
        cr = self.env.cr

        def rows_for(doc_types, side):
            if not doc_types:
                return []
            acc_col = 'debit_account' if side == 'debit' else 'credit_account'
            cr.execute(f"""
                SELECT {acc_col}, seller_code, MAX(seller_name), SUM(amount),
                       COUNT(*)
                  FROM ksw_bas_ledger_line
                 WHERE ftype = ANY(%s) AND date >= %s AND date < %s
                   AND COALESCE({acc_col}, '') != ''
              GROUP BY 1, 2
            """, (doc_types, start, end))
            return cr.fetchall()

        def build(kind, signed_rows):
            agg = defaultdict(lambda: [0.0, 0, ''])
            for account, code, name, amount, count, sign in signed_rows:
                if not self._in_scope(account, cfg):
                    continue
                key = (account, code or '')
                agg[key][0] += sign * (amount or 0.0)
                agg[key][1] += count
                agg[key][2] = agg[key][2] or (name or '')
            out = []
            for (account, doc_code), (gross, count, name) in agg.items():
                if abs(gross) < 0.005:
                    continue
                customer = customers.get(account, Customer)
                code, employee, blocked = resolve(customer, kind, doc_code)
                if code != doc_code:
                    name = labels.get(code, '')
                out.append({
                    'account': account, 'customer': customer,
                    'code': code, 'rep_name': name, 'employee': employee,
                    'blocked': blocked,
                    'gross': gross, 'net': gross / div, 'count': count,
                })
            return out

        sales = build('sales', [
            r + (1,) for r in rows_for(cfg['sales_doc_types'], 'debit')
        ] + [
            r + (-1,) for r in rows_for(cfg['sales_return_doc_types'],
                                        'credit')
        ])
        # Receipts credit the customer; a reversed receipt debits it back.
        collections = build('collection', [
            r + (1,) for r in rows_for(cfg['collection_doc_types'], 'credit')
        ] + [
            r + (-1,) for r in rows_for(cfg['collection_doc_types'], 'debit')
        ])
        targets = self._period_targets(start, cfg, customers, resolve)
        self._stamp_types(sales, collections, targets, customers, resolve)
        return {
            'cfg': cfg, 'start': start, 'end': end,
            'sales': sales, 'collections': collections, 'targets': targets,
        }

    @api.model
    def _stamp_types(self, sales, collections, targets, customers, resolve):
        """Give every row its commission type, as the workbook does:

        * **both** (Salesman & Collector) — the same employee sold to the
          customer this month and collects from him;
        * **sales** (Salesman) — he sold, somebody else collects;
        * **collect** (Collector) — he collects, somebody else sold.

        The salesman of a customer for the month is whoever its invoices
        were credited to (else the customer's own salesman in BAS); its
        collector is whoever its receipts / target were credited to (else
        the customer's own collector). A person can therefore sit on two or
        three types in one month, each with its own customers and rule.
        """
        Customer = self.env['ksw.bas.customer']

        def main(rows):
            best = {}
            for r in rows:
                acc = r['account']
                if r['employee'] and (acc not in best
                                      or abs(r['net']) > best[acc][0]):
                    best[acc] = (abs(r['net']), r['employee'])
            return {a: e for a, (_n, e) in best.items()}

        seller = main(sales)
        collector = main(collections)
        for acc, emp in main(targets).items():
            collector.setdefault(acc, emp)

        def fallback(acc, kind):
            customer = customers.get(acc, Customer)
            return resolve(customer, kind, '')[1]

        for r in sales:
            acc = r['account']
            coll = collector.get(acc) or fallback(acc, 'collection')
            r['ctype'] = ('both' if r['employee'] and coll == r['employee']
                          else 'sales')
        for r in collections + targets:
            acc = r['account']
            sold = seller.get(acc) or fallback(acc, 'sales')
            r['ctype'] = ('both' if r['employee'] and sold == r['employee']
                          else 'collect')

    # ------------------------------------------------------------------
    # Collection target
    # ------------------------------------------------------------------
    @api.model
    def _term_months(self, customer, cfg):
        days = 0
        if customer:
            days = (customer.invoice_term_days
                    if cfg['term_source'] == 'invdays'
                    else customer.credit_term_days)
        days = days or cfg['default_term_days'] or 30
        return days, max(1, round(days / 30.0))

    @api.model
    def _period_targets(self, start, cfg, customers, resolve):
        labels = self._rep_labels()
        return self._period_targets_rows(start, cfg, customers, resolve,
                                         labels)

    @api.model
    def _rep_labels(self):
        return {r.code: r.bas_name or ''
                for r in self.env['ksw.bas.sales.rep'].sudo().search([])}

    @api.model
    def _period_targets_rows(self, start, cfg, customers, resolve, labels):
        """Per customer: what is due to be collected in the month starting
        ``start`` — balance at the end of the previous month less the
        invoices of the last ``term/30`` months, never below zero.
        """
        if cfg['target_method'] != 'due_balance':
            return []
        cr = self.env.cr
        # Balance movements before the month, per account.
        cr.execute("""
            SELECT account, SUM(amt) FROM (
                SELECT debit_account AS account, amount AS amt
                  FROM ksw_bas_ledger_line
                 WHERE date < %(s)s AND COALESCE(debit_account, '') != ''
                UNION ALL
                SELECT credit_account, -amount
                  FROM ksw_bas_ledger_line
                 WHERE date < %(s)s AND COALESCE(credit_account, '') != ''
            ) m GROUP BY account
        """, {'s': start})
        moved = dict(cr.fetchall())
        # Invoices per account and month, for the not-yet-due part.
        cr.execute("""
            SELECT debit_account, date_trunc('month', date)::date, SUM(amount)
              FROM ksw_bas_ledger_line
             WHERE ftype = ANY(%s) AND date < %s
               AND COALESCE(debit_account, '') != ''
          GROUP BY 1, 2
        """, (cfg['sales_doc_types'] or [''], start))
        invoiced = defaultdict(dict)
        for account, month, amount in cr.fetchall():
            invoiced[account][month] = amount or 0.0
        # Who collected it last (on or before the end of the month).
        last_collector = {}
        if cfg['target_owner'] == 'last_receipt' and cfg['collection_doc_types']:
            cr.execute("""
                SELECT DISTINCT ON (credit_account) credit_account, seller_code
                  FROM ksw_bas_ledger_line
                 WHERE ftype = ANY(%s) AND date < %s
                   AND COALESCE(credit_account, '') != ''
                   AND COALESCE(seller_code, '') != ''
              ORDER BY credit_account, date DESC, id DESC
            """, (cfg['collection_doc_types'],
                  start + relativedelta(months=1)))
            last_collector = dict(cr.fetchall())

        div = cfg['vat_divisor']
        Customer = self.env['ksw.bas.customer']
        out = []
        accounts = set(moved) | {
            a for a, c in customers.items() if c.opening_balance}
        for account in accounts:
            if not self._in_scope(account, cfg):
                continue
            customer = customers.get(account, Customer)
            balance = (customer.opening_balance if customer else 0.0) \
                + (moved.get(account) or 0.0)
            term_days, months = self._term_months(customer, cfg)
            cut = start - relativedelta(months=months)
            not_due = sum(amt for m, amt in invoiced[account].items()
                          if m >= cut)
            target = max(0.0, balance - not_due)
            if target < 0.005 and abs(balance) < 0.005:
                continue
            code, employee, blocked = resolve(
                customer, 'collection', last_collector.get(account, ''))
            out.append({
                'account': account, 'customer': customer, 'code': code,
                'rep_name': labels.get(code, ''), 'employee': employee,
                'blocked': blocked,
                'balance': balance, 'not_due': not_due,
                'term_days': term_days,
                'gross': target, 'net': target / div,
            })
        return out

    @api.model
    def _employee_collection_target(self, employee, period, ctype=None):
        """Net collection target for one employee and month — of one
        commission type when ``ctype`` is given."""
        figures = self._period_figures(period)
        return sum(t['net'] for t in figures['targets']
                   if t['employee'] == employee
                   and (ctype is None or t.get('ctype') == ctype))
