"""The payslip batch as a BAS journal entry.

One block per employee, in the shape the accountant used to type by hand:
his earnings debited to the salary-expense accounts their rules name, what
was collected from him credited (GOSI, a penalty, his loan installments to
his own BAS loan account), and the net credited to salaries payable.

Which account a figure belongs to is configured on the salary rule
(``x_bas_posting`` + ``x_bas_account_code``) — see
:mod:`hr_salary_rule`. Nothing here infers an account from a rule's
category: GROSS and NET are totals of other rules, and a journal that
posted a total next to its own components would double every payslip.

The guard that matters is :meth:`_bas_journal_rows`'s per-employee net
check. A rule left unmapped does not unbalance the entry — the payable
line is derived as the residue, so the file would still add up while
paying the man a different net than his payslip says. The check compares
the two and refuses the export by name instead.
"""
from odoo import _, models
from odoo.exceptions import UserError

# Totals, not postings: never a journal line of their own.
TOTAL_CODES = ('GROSS', 'NET')


class HrPayslipRun(models.Model):
    _inherit = 'hr.payslip.run'

    # ------------------------------------------------------------------
    # Rows
    # ------------------------------------------------------------------
    def _bas_journal_slips(self):
        """The payslips this entry posts: everything the batch still pays.

        A cancelled payslip was pulled back, so it is not in the entry —
        the same rule the bank file follows.
        """
        self.ensure_one()
        return self.slip_ids.filtered(lambda s: s.state != 'cancel')

    def _bas_journal_rows(self):
        """Every journal line for this batch, employee by employee."""
        self.ensure_one()
        Journal = self.env['ksw.bas.journal']
        month = Journal.month_label(self.date_end or self.date_start)
        rows = []
        unmapped = {}
        self._check_loan_accounts()

        for slip in self._bas_journal_slips().sorted(
                lambda s: (s.employee_id.sudo().name or '', s.id)):
            employee = slip.employee_id.sudo()
            if not slip.line_ids:
                # A draft slip nobody has computed yet has nothing to post
                # and no NET rule to read a payable account from. Skipping
                # it beats a refusal that names an account problem the
                # batch does not have.
                continue
            debits, credits = [], []
            payable = self._bas_payable_account(slip)
            expected_net = self._get_line_total(slip, 'NET')

            for line in slip.line_ids.sorted(lambda l: l.sequence):
                amount = line.total or 0.0
                if not amount:
                    continue
                rule = line.salary_rule_id
                posting = rule.x_bas_posting
                ref = Journal.ref(line.name or rule.name, employee.name,
                                  month)
                if posting == 'expense':
                    debits.append({
                        'code': rule.x_bas_account_code,
                        'name': rule.x_bas_account_name,
                        'amount': amount,
                        'ref': ref,
                        'credit_code': payable[0],
                        'credit_name': payable[1],
                        'credit_ref': Journal.ref(
                            _('Salary'), employee.name, month),
                    })
                elif posting == 'liability':
                    credits.append({
                        'code': rule.x_bas_account_code,
                        'name': rule.x_bas_account_name,
                        'amount': -amount,
                        'ref': ref,
                    })
                elif posting == 'loan':
                    code, name = self._bas_loan_account(employee)
                    credits.append({
                        'code': code, 'name': name, 'amount': -amount,
                        'ref': Journal.ref(
                            _('Deducted from salary'), month),
                    })
                elif posting == 'none' and line.code not in TOTAL_CODES:
                    unmapped.setdefault(
                        rule.code or rule.name or '?', rule.name or '')

            posted_net = round(
                sum(d['amount'] for d in debits)
                - sum(c['amount'] for c in credits), 2)
            if abs(posted_net - round(expected_net, 2)) >= 0.005:
                raise UserError(_(
                    "%(employee)s's payslip nets %(net).2f but the journal "
                    "entry would post %(posted).2f. A salary rule with an "
                    "amount on this payslip has no BAS posting configured, "
                    "so its figure is missing from the entry.\n\n"
                    "Unmapped rules on this batch: %(rules)s\n\n"
                    "Set the BAS Posting and account on each of them "
                    "(Payroll ▸ Configuration ▸ Salary Rules) and export "
                    "again.",
                    employee=employee.name or '',
                    net=expected_net, posted=posted_net,
                    rules=', '.join(sorted(unmapped)) or _('none found')))

            rows += Journal.employee_block(debits, credits)

        if not rows:
            raise UserError(_(
                'There is nothing to post — this batch has no payslip with '
                'an amount on it.'))
        return rows

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------
    def _bas_payable_account(self, slip):
        """The account the employee's net becomes a liability on.

        Read from whichever rule on his own structure is marked *net
        payable* — normally NET. Per payslip rather than per batch because
        two structures may credit different payable accounts.
        """
        rule = slip.line_ids.salary_rule_id.filtered(
            lambda r: r.x_bas_posting == 'payable')[:1]
        if not rule:
            raise UserError(_(
                'No salary rule is marked "Credit — net payable to the '
                "employee\", so there is no account to credit %(name)s's "
                'net to. Set the BAS Posting on the NET rule (Payroll ▸ '
                'Configuration ▸ Salary Rules).',
                name=slip.employee_id.sudo().name or ''))
        if not rule.x_bas_account_code:
            raise UserError(_(
                'The salary rule "%(rule)s" is marked net payable but has '
                'no BAS Account Code.', rule=rule.name or rule.code))
        return rule.x_bas_account_code, rule.x_bas_account_name

    def _check_loan_accounts(self):
        """Everyone whose repayment has nowhere to go, before row one."""
        self.ensure_one()
        owing = self.env['hr.employee']
        for slip in self._bas_journal_slips():
            if any(line.total and line.salary_rule_id.x_bas_posting == 'loan'
                   for line in slip.line_ids):
                owing |= slip.employee_id
        if owing:
            self.env['ksw.bas.journal'].check_loan_accounts(owing.sudo())

    def _bas_loan_account(self, employee):
        """The employee's BAS loan account — defined once, in the writer."""
        return self.env['ksw.bas.journal'].loan_account(employee)

    # ------------------------------------------------------------------
    # The file
    # ------------------------------------------------------------------
    def _bas_journal_workbook(self):
        """The batch's journal entry, as .xlsx bytes."""
        self.ensure_one()
        Journal = self.env['ksw.bas.journal']
        return Journal.build_workbook(
            self._bas_journal_rows(),
            self.date_end or self.date_start,
            sheet_name=self._bas_journal_sheet_name(),
        )

    def _bas_journal_sheet_name(self):
        self.ensure_one()
        date = self.date_end or self.date_start
        return '%s %s' % (_('Journal'),
                          date.strftime('%m-%Y') if date else '')
