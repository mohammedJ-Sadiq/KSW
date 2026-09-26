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

from odoo import _, fields, models
from odoo.exceptions import UserError


class KswPayRun(models.Model):
    _inherit = 'ksw.pay.run'

    # ------------------------------------------------------------------
    # Source figures
    # ------------------------------------------------------------------
    @staticmethod
    def _entry_note(entry):
        """What the *supervisor* wrote on this entry, as one line.

        The reason and the further details are two fields on the screen
        and one sentence on a voucher. Either may be empty — and on an
        imported component ``details`` is not his at all: the BAS importer
        writes its own audit trail there ("Weighted on «الرد المضاعف»
        (customer rate). Worked days: 31. Required trips before earning:
        50."). That is worth keeping on the entry and worthless on a
        journal line, so those fall back to the component's own name,
        which is what the hand-typed voucher used.
        """
        parts = [(entry.reason or '').strip()]
        if not entry.component_id.importer:
            parts.append((entry.details or '').strip())
        return ' - '.join(part for part in parts if part)

    def _bas_component_totals(self):
        """``{employee_id: {(component, note): amount}}`` for the month.

        Same entries as ``_build_register``: approved batches only, minus
        anything a vacation hold is keeping back.

        Keyed on the supervisor's note as well as the component, so the
        voucher carries **his own words** — "تصليح قفل باب سيارة ١٢٤" says
        what a month of "بدل عمل اضافى" never could, and the accountant
        reading it in BAS is the person who would otherwise have to come
        back and ask. Occurrences he described the same way are still one
        line; the note is what splits them, not the date.
        """
        self.ensure_one()
        entries = self.env['ksw.pay.entry'].sudo().browse()
        for batch in self._payable_batches(settled_only=True):
            entries |= batch.sudo().entry_ids
        entries = entries.filtered('employee_id')
        payable = entries - self._held_entries(entries)

        totals = {}
        for entry in payable.sorted(
                lambda e: (e.component_id.sequence, e.component_id.id,
                           e.date or fields.Date.today(), e.id)):
            by_line = totals.setdefault(entry.employee_id.id, {})
            key = (entry.component_id, self._entry_note(entry))
            by_line[key] = round(
                by_line.get(key, 0.0) + (entry.amount or 0.0), 2)
        return totals

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
            for (component, _note), amount in by_line.items():
                if amount:
                    used |= component
        self._check_component_accounts(used)
        Journal.check_loan_accounts(self.line_ids.filtered('loan_offset')
                                    .employee_id.sudo())
        rows = []
        mismatched = []

        for line in self.line_ids.sorted(
                lambda l: (l.employee_id.sudo().name or '', l.id)):
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
            for (component, note), amount in by_line.items():
                debits.append({
                    'code': component.x_bas_expense_code,
                    'name': component.x_bas_expense_name,
                    'amount': amount,
                    # His note in place of the component's name when he
                    # wrote one: it is the more specific answer to the same
                    # question, and it is already in the language the
                    # voucher is read in.
                    'ref': Journal.ref(note or component.name, who, month),
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
                        _('Monthly commissions'), who, month),
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
