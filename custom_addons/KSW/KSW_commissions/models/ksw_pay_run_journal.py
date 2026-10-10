"""The month as a BAS journal entry.

The accountant's last manual step: every approved commission and allowance
retyped into BAS, line by line, once a month. This builds the same entry as
a file BAS imports — one block per employee, in the order the hand-typed
voucher had it:

    3203020007  commissions expense   2,685          ايسوزو356
    2107010001  commissions accrual           2,685
    2107010001  commissions accrual   2,000
    1205010385  his loan account               2,000

Gross, not netted (Oct 2026, the user's rule): the commission is first
credited to the accrual pool in full — the employee's right to it — and
every deduction taken out of it is its own record, Dr accrual / Cr his
advance. A deduction that existed on the month end is in this voucher; one
charged later is a voucher of its own, dated the day it was charged
(``_bas_recovery_rows``) — never back-dated into the month.

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

from odoo import _, api, models
from odoo.exceptions import UserError


class KswPayRun(models.Model):
    _inherit = 'ksw.pay.run'

    # ------------------------------------------------------------------
    # Source figures
    # ------------------------------------------------------------------
    def _bas_component_totals(self):
        """``{employee_id: {component: whole riyals}}`` for the month.

        Same entries as ``_build_register``, and the same rounding
        (``_rounded_component_totals``): approved batches only, minus
        anything a vacation hold is keeping back.

        One figure per employee per pay type. The voucher says *what* was
        paid and for which month, not each occurrence the supervisor
        recorded — a month of overtime is one line, however many rows and
        notes it was entered as, and the Fridays one line however they
        were split.
        """
        self.ensure_one()
        entries = self._payable_entries(
            settled_only=True).filtered('employee_id')
        # Exactly the register's set: an entry paid on a vacation payslip is
        # not in the register either, and leaving it in here made the
        # employee's journal figure disagree with his register line.
        payable = (entries - self._held_entries(entries)
                   - entries.filtered('x_vacation_payslip_id'))
        return self._rounded_component_totals(payable)

    def _vacation_settled_totals(self):
        """``{employee: (entries, {component: whole riyals})}`` for the
        month's entries already paid on a vacation payslip.

        Not payable here — the Excel summary lists them, marked, so the
        month can be reconciled without asking where they went.
        """
        self.ensure_one()
        settled = self._all_entries().filtered(
            lambda e: e.employee_id and e.x_vacation_payslip_id)
        totals = self._rounded_component_totals(settled)
        return {
            employee: (settled.filtered(lambda e, emp=employee: e.employee_id == emp),
                       totals.get(employee.id, {}))
            for employee in settled.employee_id
        }

    @api.model
    def _bas_detail_lines(self, by_component):
        """One employee's month as the voucher itemises it.

        ``by_component`` is his ``{component: amount}``. Components the
        voucher describes the same way *and* posts to the same accounts
        are one line — "Other" and every bonus are all «بدل عمل اضافي» on
        3201010006 / 2107010001, and three lines saying the same thing
        against the same account is detail the voucher does not want.
        The Excel summary reads this too, so its columns are the voucher's.

        Returns a list of ``{'label', 'component', 'amount'}`` in catalog
        order; ``component`` is the first of the group and carries the
        accounts.
        """
        lines = {}
        for component, amount in by_component.items():
            label = (component.x_bas_ref or component.name or '').strip()
            key = (label, component.x_bas_expense_code,
                   component.x_bas_accrual_code,
                   component.x_bas_use_cost_center)
            if key not in lines:
                lines[key] = {'label': label, 'component': component,
                              'amount': 0.0}
            lines[key]['amount'] += amount
        return list(lines.values())

    def _bas_ref(self, label, who, month):
        """``<what> <who> شهر <month>`` — the description on every line.

        The name is BAS's own spelling of the employee
        (``ksw.bas.journal.employee_label``): with one line per pay type,
        it is the only thing that tells two drivers' lines apart.
        """
        return self.env['ksw.bas.journal'].ref(label, who, _('month'), month)

    # ------------------------------------------------------------------
    # Rows
    # ------------------------------------------------------------------
    def _bas_journal_rows(self):
        """Every journal line for this month, employee by employee."""
        self.ensure_one()
        Journal = self.env['ksw.bas.journal']
        # Every word below — the component names, the month, the _() terms
        # — resolves against the context language, so the voucher is built
        # in the language it is read in rather than the one the exporter
        # happens to use. One re-entry, then the real work.
        lang = Journal.voucher_lang()
        if lang and self.env.context.get('lang') != lang:
            return self.with_context(lang=lang)._bas_journal_rows()
        month = Journal.month_label(self.period)
        totals = self._bas_component_totals()
        # Every component in one pass, before any row is built: hitting
        # this one component at a time, re-running the export after each,
        # is the same whack-a-mole the BAS importer was taught not to play.
        used = self.env['ksw.pay.component']
        for by_line in totals.values():
            for component, amount in by_line.items():
                if amount:
                    used |= component
        self._check_component_accounts(used)
        Journal.check_loan_accounts(self.line_ids.filtered('loan_offset')
                                    .employee_id.sudo())
        rows = []
        mismatched = []

        for line in self.line_ids._export_sorted():
            employee = line.employee_id.sudo()
            by_line = totals.get(employee.id, {})
            who = Journal.employee_label(employee)
            earnings = round(sum(by_line.values()), 2)
            if abs(earnings - round(line.earnings or 0.0, 2)) >= 0.005:
                mismatched.append('• %s — %s %.2f, %s %.2f' % (
                    employee.name or '', _('register'), line.earnings or 0.0,
                    _('entries'), earnings))
                continue

            debits = []
            for detail in self._bas_detail_lines(by_line):
                component = detail['component']
                debits.append({
                    'code': component.x_bas_expense_code,
                    'name': component.x_bas_expense_name,
                    'amount': detail['amount'],
                    'ref': self._bas_ref(detail['label'], who, month),
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
                    'credit_ref': self._bas_ref(
                        _('Monthly commissions'), who, month),
                })

            rows += Journal.employee_block(debits)
            rows += self._bas_offset_rows(line, by_line, who, month).get(
                self._bas_journal_date(), [])

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
    # Recoveries — the second voucher
    # ------------------------------------------------------------------
    def _bas_offset_rows(self, line, by_component, who, month):
        """``{posting date: rows}`` — what was taken out of one employee's
        commission, one Dr accrual / Cr advance record per deduction.

        Each installment settled by ``line`` posts on the later of the month
        end (when the accrual exists) and the day its deduction was charged
        (when the debt exists). The accrual debited is his own, taken in
        the voucher's order across his pay types.
        """
        Journal = self.env['ksw.bas.journal']
        month_end = self._bas_journal_date()
        groups, index = [], {}
        for detail in self._bas_detail_lines(by_component):
            component = detail['component']
            code = component.x_bas_accrual_code or ''
            if code not in index:
                index[code] = len(groups)
                groups.append([code, component.x_bas_accrual_name or '', 0.0])
            groups[index[code]][2] += detail['amount']

        # (date, deduction name) -> amount, in posting order.
        offsets = {}
        settled = self.env['ksw.deduction.line'].sudo().search([
            ('x_paid_via_pay_run_line_id', '=', line.id),
            ('state', '=', 'paid'),
        ])
        for ded_line in settled:
            ded = ded_line.deduction_id
            when = max(d for d in (month_end, ded.x_charge_date) if d)
            key = (when, ded.name or '')
            offsets[key] = offsets.get(key, 0.0) + ded_line.amount
        # A register line carried over from the old commission sheets can
        # hold an offset with no installment linked to it: still his, still
        # on the month.
        unlinked = round((line.loan_offset or 0.0)
                         - sum(settled.mapped('amount')), 2)
        if unlinked > 0.0:
            key = (month_end, '')
            offsets[key] = offsets.get(key, 0.0) + unlinked

        if not offsets:
            return {}
        loan_code, loan_name = self._bas_loan_account(line.employee_id.sudo())
        by_date = {}
        for (when, ded_name), amount in sorted(offsets.items()):
            ref = self._bas_ref(
                Journal.ref(_('Deducted from commissions'), ded_name),
                who, month)
            rows = by_date.setdefault(when, [])
            left = round(amount, 2)
            for group in groups:
                if left <= 0.0:
                    break
                take = round(min(group[2], left), 2)
                if take <= 0.0:
                    continue
                rows.append(Journal.row(group[0], group[1], debit=take,
                                        ref=ref))
                group[2] -= take
                left = round(left - take, 2)
            if left > 0.0:
                raise UserError(_(
                    '%(name)s: more was deducted from his %(month)s commission '
                    'than he earned in it, so there is no accrual left to take '
                    'it from.', name=line.employee_id.sudo().name or '',
                    month=self.display_name))
            rows.append(Journal.row(loan_code, loan_name, credit=amount,
                                    ref=ref))
        return by_date

    def _bas_recovery_rows(self):
        """``{posting date: rows}`` for the deductions charged after the
        month end — each date one voucher of its own."""
        self.ensure_one()
        Journal = self.env['ksw.bas.journal']
        lang = Journal.voucher_lang()
        if lang and self.env.context.get('lang') != lang:
            return self.with_context(lang=lang)._bas_recovery_rows()
        month = Journal.month_label(self.period)
        month_end = self._bas_journal_date()
        totals = self._bas_component_totals()
        by_date = {}
        for line in self.line_ids.filtered('loan_offset')._export_sorted():
            employee = line.employee_id.sudo()
            offsets = self._bas_offset_rows(
                line, totals.get(employee.id, {}),
                Journal.employee_label(employee), month)
            for when, rows in offsets.items():
                if when != month_end:
                    by_date.setdefault(when, []).extend(rows)
        return by_date

    def _bas_recovery_workbooks(self, number=''):
        """``[(filename, bytes)]`` — one voucher per posting date."""
        self.ensure_one()
        by_date = self._bas_recovery_rows()
        if not by_date:
            raise UserError(_(
                'Nothing was deducted from the %(month)s commission after '
                'its month end.',
                month=self.display_name))
        number = (number or '').strip()
        Journal = self.env['ksw.bas.journal']
        files = []
        for when in sorted(by_date):
            data = Journal.build_workbook(
                by_date[when], when,
                sheet_name='%s %s' % (_('Recoveries'),
                                      when.strftime('%d-%m-%Y')),
                number=int(number) if number.isdigit() else number,
            )
            files.append(('CommissionRecoveries_%s.xlsx'
                          % when.strftime('%Y-%m-%d'), data))
        return files

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------
    def _check_component_accounts(self, components):
        """Refuse by name rather than post a half entry.

        Takes the whole month's components at once and lists every one
        that is short an account, so the accountant fills them in in a
        single visit to the catalog instead of discovering them one
        export at a time.
        """
        unmapped = []
        for component in components:
            missing = [label for label, value in (
                (_('BAS Expense Account'), component.x_bas_expense_code),
                (_('BAS Accrual Account'), component.x_bas_accrual_code),
            ) if not value]
            if missing:
                unmapped.append('• %s — %s' % (component.name or '',
                                               ' / '.join(missing)))
        if unmapped:
            raise UserError(_(
                'These pay components have no BAS account set, so their '
                'amounts have nothing to be posted to:\n\n%(missing)s\n\n'
                'Set them on each component (Commissions ▸ Configuration ▸ '
                'Pay Components ▸ BAS Journal) and export again.',
                missing='\n'.join(unmapped)))

    def _bas_loan_account(self, employee):
        """The employee's BAS loan account — defined once, in the writer."""
        return self.env['ksw.bas.journal'].loan_account(employee)

    # ------------------------------------------------------------------
    # The file
    # ------------------------------------------------------------------
    def _bas_journal_workbook(self, number=''):
        """This month's journal entry, as .xlsx bytes.

        ``number`` is the voucher number the accountant reserved in BAS,
        written on every line; a number in digits is written as a number,
        like ``fcode``.
        """
        self.ensure_one()
        number = (number or '').strip()
        return self.env['ksw.bas.journal'].build_workbook(
            self._bas_journal_rows(),
            self._bas_journal_date(),
            sheet_name=self._bas_journal_sheet_name(),
            number=int(number) if number.isdigit() else number,
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
