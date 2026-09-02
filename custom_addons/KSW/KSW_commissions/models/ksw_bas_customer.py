"""Extends ksw.bas.customer (KSW_ext_sync) with employee-rep resolution
and BAS AR-aging-derived collection targets.

Lives here, not in KSW_ext_sync, because it needs ``hr.employee`` and
``hr.employee.x_commission_import_name`` — both owned by this module.
KSW_ext_sync stays a pure read-only BAS sync module with no HR/commission
concerns; KSW_commissions (which already depends on KSW_ext_sync) adds the
business logic on top via ``_inherit``.
"""
from collections import defaultdict, deque

from odoo import api, fields, models

_DEFAULT_CREDIT_TERM_DAYS = 30


class KswBasCustomer(models.Model):
    _inherit = 'ksw.bas.customer'

    effective_sales_rep_id = fields.Many2one(
        'hr.employee', string='Effective Sales Rep', readonly=True,
        help='The employee actually credited with sales commission on '
             'this customer: partner_id.x_sales_rep_id if explicitly set '
             '(manual override), else seller_name resolved to an '
             'hr.employee via x_commission_import_name / name. Recomputed '
             'on every sync.',
    )
    effective_collector_id = fields.Many2one(
        'hr.employee', string='Effective Collection Rep', readonly=True,
        help='Same resolution as effective_sales_rep_id, for collection '
             '(x_collection_rep_id override, else collector_name).',
    )

    @api.model
    def sync_from_bas(self, deadline=None, commit=True):
        # Signature and return value must track KSW_ext_sync's: the
        # orchestrator passes a time budget and reads back whether the pass
        # completed, so it knows not to advance the watermark.
        done = super().sync_from_bas(deadline=deadline, commit=commit)
        self._recompute_effective_reps()
        return done

    @api.model
    def action_match_or_create_partners(self, *args, **kwargs):
        result = super().action_match_or_create_partners(*args, **kwargs)
        self._recompute_effective_reps()
        return result

    # ------------------------------------------------------------------
    # Effective rep resolution — shared by the commission BAS-pull and
    # the collection-target aging computation below, so both always agree
    # on "who is this customer's rep" instead of duplicating the logic.
    # ------------------------------------------------------------------
    def _recompute_effective_reps(self):
        """Resolve + store effective_sales_rep_id / effective_collector_id
        for every synced customer: a manual override on the linked
        partner (x_sales_rep_id / x_collection_rep_id) wins; otherwise
        resolve BAS's own seller_name / collector_name to an hr.employee
        via x_commission_import_name (falling back to the employee's
        plain name) — the same alias field/priority the Excel wizard uses.
        """
        Employee = self.env['hr.employee']
        employees = Employee.search([('active', '=', True)])
        alias_map = {}
        name_map = {}
        for emp in employees:
            alias = (emp.x_commission_import_name or '').strip().lower()
            if alias:
                alias_map[alias] = emp
            name_map[emp.name.strip().lower()] = emp

        def _resolve(name):
            key = (name or '').strip().lower()
            if not key:
                return Employee.browse()
            return alias_map.get(key) or name_map.get(key) or Employee.browse()

        for rec in self.search([]):
            override_sales = (
                rec.partner_id.x_sales_rep_id
                if rec.partner_id else Employee.browse())
            override_coll = (
                rec.partner_id.x_collection_rep_id
                if rec.partner_id else Employee.browse())
            eff_sales = override_sales or _resolve(rec.seller_name)
            eff_coll = override_coll or _resolve(rec.collector_name)
            vals = {}
            if rec.effective_sales_rep_id != eff_sales:
                vals['effective_sales_rep_id'] = eff_sales.id
            if rec.effective_collector_id != eff_coll:
                vals['effective_collector_id'] = eff_coll.id
            if vals:
                rec.write(vals)

    # ------------------------------------------------------------------
    # Collection target from BAS AR aging — see gotcha #76: BAS is a
    # balance-forward ledger with no per-invoice paid/unpaid flag, and its
    # own stored running-balance fields (COD10.DOLDACC/DDACC/DCACC) proved
    # unreliable (381/762 customers all-zero, majority of the rest
    # negative for an AR ledger). So the "how much is overdue" figure is
    # derived here directly from this fiscal year's invoice/payment
    # history via FIFO matching, not trusted from BAS's stored fields.
    # ------------------------------------------------------------------
    @api.model
    def _compute_collection_target(self, employee, as_of_date):
        """Sum of the aged (overdue, past credit_term_days) open balance
        across every BAS customer whose effective collector is
        ``employee``, as of ``as_of_date`` (a date, typically a period
        end). Returns 0.0 if the employee collects for no customer.
        """
        customers = self.search([('effective_collector_id', '=', employee.id)])
        if not customers:
            return 0.0
        codes = customers.mapped('bas_code')
        term_by_code = {
            c.bas_code: (c.credit_term_days or _DEFAULT_CREDIT_TERM_DAYS)
            for c in customers
        }

        invoices = self.env['ksw.bas.invoice'].search([
            ('from_account', 'in', codes),
            ('invoice_date', '<=', as_of_date),
        ], order='invoice_date asc')
        payments = self.env['ksw.bas.payment'].search([
            ('to_account', 'in', codes),
            ('payment_date', '<=', as_of_date),
        ], order='payment_date asc')

        inv_by_code = defaultdict(list)
        for inv in invoices:
            inv_by_code[inv.from_account].append(
                (inv.invoice_date, inv.subtotal + inv.tax_amount))
        pay_by_code = defaultdict(list)
        for pay in payments:
            pay_date = (
                pay.payment_date.date() if hasattr(pay.payment_date, 'date')
                else pay.payment_date)
            pay_by_code[pay.to_account].append((pay_date, pay.amount))

        total_aged = 0.0
        for code in codes:
            total_aged += self._fifo_aged_amount(
                inv_by_code.get(code, []), pay_by_code.get(code, []),
                term_by_code[code], as_of_date)
        return total_aged

    @staticmethod
    def _fifo_aged_amount(invoices, payments, term_days, as_of_date):
        """FIFO-match a single customer's invoices against payments and
        return the portion of the still-open balance whose originating
        invoice is older than ``term_days`` as of ``as_of_date``.

        Payments settle the OLDEST open invoice first (standard AR
        aging assumption for a balance-forward ledger with no per-invoice
        matching). A payment that exceeds all currently-open invoices
        becomes unapplied credit, netted against the next invoice that
        arrives — this can happen with advance payments.
        """
        events = sorted(
            [(d, amt, 'inv') for d, amt in invoices]
            + [(d, amt, 'pay') for d, amt in payments],
            key=lambda e: e[0])
        queue = deque()  # [[invoice_date, remaining_amount], ...]
        unapplied_credit = 0.0
        for date, amt, kind in events:
            if kind == 'inv':
                if unapplied_credit > 0:
                    consume = min(unapplied_credit, amt)
                    unapplied_credit -= consume
                    amt -= consume
                if amt > 0.01:
                    queue.append([date, amt])
            else:
                remaining = amt
                while remaining > 0.01 and queue:
                    chunk = queue[0]
                    take = min(chunk[1], remaining)
                    chunk[1] -= take
                    remaining -= take
                    if chunk[1] <= 0.01:
                        queue.popleft()
                if remaining > 0.01:
                    unapplied_credit += remaining

        aged = 0.0
        for date, remaining in queue:
            if remaining <= 0.01:
                continue
            if (as_of_date - date).days > term_days:
                aged += remaining
        return aged
