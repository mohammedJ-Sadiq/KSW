"""The month as a BAS journal entry.

The accountant's last manual step: every approved commission and allowance
retyped into BAS, line by line, once a month. This builds the same entry as
a file BAS imports — one block per employee, in the order the hand-typed
voucher had it:

    3203020007  commissions expense   2,685          ايسوزو356
    1205010385  his loan account               2,000
    2107010001  commissions accrual             685

The accounts come from the pay component (see
:class:`ksw.pay.component`), the loan account from the employee's own
``Loan Acc No. in Bas``, and the writer from :class:`ksw.bas.journal` in
KSW_payroll — the payslip batch posts through the same one.

The figures come from the entries the *register* was built from, not from
a fresh search: the register is what the month paid, and an entry that
posts a different set of numbers than the bank file is worse than none.
Where the two disagree the export stops and says whose figures they are.
"""
from dateutil.relativedelta import relativedelta

from odoo import _, models
from odoo.exceptions import UserError


class KswPayRun(models.Model):
    _inherit = 'ksw.pay.run'

    # ------------------------------------------------------------------
    # Source figures
    # ------------------------------------------------------------------
    def _bas_component_totals(self):
        """``{employee_id: {component: amount}}`` for the payable month.

        Same entries as ``_build_register``: approved batches only, minus
        anything a vacation hold is keeping back.
        """
        self.ensure_one()
        entries = self.env['ksw.pay.entry'].sudo().browse()
        for batch in self._payable_batches(settled_only=True):
            entries |= batch.sudo().entry_ids
        entries = entries.filtered('employee_id')
        payable = entries - self._held_entries(entries)

        totals = {}
        for entry in payable.sorted(
                lambda e: (e.component_id.sequence, e.component_id.id)):
            by_component = totals.setdefault(entry.employee_id.id, {})
            component = entry.component_id
            by_component[component] = round(
                by_component.get(component, 0.0) + (entry.amount or 0.0), 2)
        return totals

    # ------------------------------------------------------------------
    # Rows
    # ------------------------------------------------------------------
    def _bas_journal_rows(self):
        """Every journal line for this month, employee by employee."""
        self.ensure_one()
        Journal = self.env['ksw.bas.journal']
        month = Journal.month_label(self.period)
        totals = self._bas_component_totals()
        rows = []
        mismatched = []

        for line in self.line_ids.sorted(
                lambda l: (l.employee_id.sudo().name or '', l.id)):
            employee = line.employee_id.sudo()
            by_component = totals.get(employee.id, {})
            earnings = round(sum(by_component.values()), 2)
            if abs(earnings - round(line.earnings or 0.0, 2)) >= 0.005:
                mismatched.append('• %s — %s %.2f, %s %.2f' % (
                    employee.name or '', _('register'), line.earnings or 0.0,
                    _('entries'), earnings))
                continue

            debits = []
            for component, amount in by_component.items():
                self._check_component_accounts(component)
                debits.append({
                    'code': component.x_bas_expense_code,
                    'name': component.x_bas_expense_name,
                    'amount': amount,
                    'ref': Journal.ref(component.name, employee.name,
                                       month),
                    # The *vehicle*, never x_bas_driver_cost_center: that
                    # one identifies the driver (BAS COST_CENTER2) and
                    # putting it in this column would post every line to a
                    # cost centre BAS does not have. Blank until his next
                    # BAS pull fills it in — an empty dimension is a
                    # nuisance, a wrong one is a wrong ledger.
                    'cost_center': (employee.x_bas_equipment_code or ''
                                    if component.x_bas_use_cost_center
                                    else ''),
                    'credit_code': component.x_bas_accrual_code,
                    'credit_name': component.x_bas_accrual_name,
                    # Not _('Commissions') — that msgid is already the
                    # app's own name ("عمولات KSW"), and a voucher line
                    # must not read like a menu.
                    'credit_ref': Journal.ref(
                        _('Monthly commissions'), employee.name, month),
                })

            credits = []
            if line.loan_offset:
                code, name = self._bas_loan_account(employee)
                credits.append({
                    'code': code, 'name': name,
                    'amount': line.loan_offset,
                    'ref': Journal.ref(
                        _('Deducted from commissions'), month),
                })
            rows += Journal.employee_block(debits, credits)

        if mismatched:
            raise UserError(_(
                'These employees are paid a figure the entries no longer '
                'add up to, so the journal entry cannot say which account '
                'the money came out of:\n\n%(who)s\n\nRebuild the register '
                '(or correct the entries) and export again.',
                who='\n'.join(mismatched[:20])))
        if not rows:
            raise UserError(_(
                'There is nothing to post — this month has no payable '
                'commission.'))
        return rows

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------
    def _check_component_accounts(self, component):
        """Refuse by name rather than post a half entry."""
        missing = [label for label, value in (
            (_('BAS Expense Account'), component.x_bas_expense_code),
            (_('BAS Accrual Account'), component.x_bas_accrual_code),
        ) if not value]
        if missing:
            raise UserError(_(
                'The pay component "%(component)s" has no %(missing)s set, '
                'so its amounts have no account to be posted to. Set it on '
                'the component (Commissions ▸ Configuration ▸ Pay '
                'Components) and export again.',
                component=component.name or '',
                missing=' / '.join(missing)))

    def _bas_loan_account(self, employee):
        """The employee's BAS loan account — defined once, in the writer."""
        return self.env['ksw.bas.journal'].loan_account(employee)

    # ------------------------------------------------------------------
    # The file
    # ------------------------------------------------------------------
    def _bas_journal_workbook(self):
        """This month's journal entry, as .xlsx bytes."""
        self.ensure_one()
        return self.env['ksw.bas.journal'].build_workbook(
            self._bas_journal_rows(),
            self._bas_journal_date(),
            sheet_name=self._bas_journal_sheet_name(),
        )

    def _bas_journal_date(self):
        """The last day of the month being paid — where the cost belongs.

        Never today: a run exported in August is still July's expense, and
        the hand-typed voucher always carried the period end.
        """
        self.ensure_one()
        if not self.period:
            return False
        return self.period.replace(day=1) + relativedelta(months=1, days=-1)

    def _bas_journal_sheet_name(self):
        self.ensure_one()
        return '%s %s' % (_('Journal'),
                          self.period.strftime('%m-%Y') if self.period else '')
