"""Paying a leave settlement: the second entry.

GM final approval makes the settlement payable: its payslip is confirmed and
booked (expenses / Cr salaries payable 2107010004, as BAS's «تصفيات» voucher
26011024). Paying it is a separate decision, the accountant's, taken on the
time off request (Nov 2026):

* **Bank transfer**: a one-person off-cycle bank file is generated on the
  spot and kept on the request; Dr payable / Cr the paying account's bank.
* **Cheque**: handing it over is the proof it was paid; Dr payable / Cr
  «شيكات صادرة لم تصرف» of the issuing bank. When the bank pays it: Dr that
  account / Cr the bank. Its balance is the money still due out of the bank.
* **Cash**: the accountant names the cash box; the Loan Disbursement Officer
  (who already hands out revision cash) confirms the handover; Dr payable /
  Cr the box.

The money record is the payslip (a vacation extension can bring a second
settlement on the same leave), the buttons are on the leave. The payment runs
next to HR's Step 6 (filing the signed form), not after it: neither waits for
the other.
"""
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.osv import expression as odoo_expr

ACC_GROUP = 'KSW_annual_leave.group_annual_leave_acc'
DISB_GROUP = 'KSW_deduction.group_loan_disbursement'
ACC_MANAGER = 'account.group_account_manager'

# 'bank' is the payroll bank file (Rajhi WPS / payroll cards: the only
# formats the exporter writes); 'transfer' is any other bank, transferred by
# hand from its online banking, no file.
METHODS = [('bank', 'Payroll Bank File'), ('transfer', 'Direct Bank Transfer'),
           ('cheque', 'Cheque'), ('cash', 'Cash')]
STATES = [
    ('none', 'Nothing to Pay'),
    ('to_choose', 'To Pay'),
    ('cash_pending', 'Waiting for Cash Handover'),
    ('cheque_handed', 'Cheque Handed, Not Cashed'),
    ('paid', 'Paid'),
]
# Which one a leave shows when it has more than one settlement.
STATE_PRIORITY = ['to_choose', 'cash_pending', 'cheque_handed', 'paid', 'none']
CHEQUES_PREFIX = '211301'   # free in BAS's chart (2113 is unused there)


class AccountJournal(models.Model):
    _inherit = 'account.journal'

    x_ledger_account_id = fields.Many2one(
        'account.account', string='Ledger Account (BAS)',
        help="The account BAS moves for this bank, when it is not the journal's own: "
             'Arab Bank pays out of its overdraft current account 2103010001, which a bank '
             'journal cannot hold. Empty: the journal account.')
    x_cheques_account_id = fields.Many2one(
        'account.account', string='Cheques Issued, Not Cashed',
        help='Credited when a cheque on this bank is handed over, debited when the bank '
             'pays it. Its balance is what is still to leave the bank. Created on the first '
             'cheque if empty.')

    def _ksw_ledger_account(self):
        self.ensure_one()
        return self.sudo().x_ledger_account_id or self.sudo().default_account_id

    def _ksw_cheques_account(self):
        """This bank's issued-cheques account, opened on its first cheque."""
        self.ensure_one()
        journal = self.sudo()
        if journal.x_cheques_account_id:
            return journal.x_cheques_account_id
        Account = self.env['account.account'].sudo().with_company(journal.company_id)
        codes = Account.search([('code', '=like', CHEQUES_PREFIX + '%'),
                                ('company_ids', 'in', journal.company_id.id)]).mapped('code')
        number = max([int(c[len(CHEQUES_PREFIX):]) for c in codes
                      if c[len(CHEQUES_PREFIX):].isdigit()] or [0]) + 1
        account = Account.create({
            'code': '%s%04d' % (CHEQUES_PREFIX, number),
            'name': 'شيكات صادرة لم تصرف - %s' % (journal.name or journal.code),
            'account_type': 'liability_current', 'reconcile': True,
            'company_ids': [(6, 0, journal.company_id.ids)],
        })
        journal.x_cheques_account_id = account
        return account


class ResPartnerBank(models.Model):
    _inherit = 'res.partner.bank'

    x_ledger_journal_id = fields.Many2one(
        'account.journal', string='Paid From (Journal)', domain="[('type', '=', 'bank')]",
        help='For a paying account of the company (Kawther Raj Cards…): the bank journal '
             'whose account a transfer made from it is credited to (Al Rajhi 1202010005).')


class HrPayslip(models.Model):
    _inherit = 'hr.payslip'

    x_disb_method = fields.Selection(METHODS, string='Payment Method', readonly=True, copy=False)
    x_disb_state = fields.Selection(
        STATES, string='Settlement Payment', compute='_compute_disb_state', store=True,
        copy=False)
    x_disb_journal_id = fields.Many2one('account.journal', string='Paid From', readonly=True, copy=False)
    x_disb_cheque_no = fields.Char(string='Cheque No.', readonly=True, copy=False)
    x_disb_cheque_date = fields.Date(string='Cheque Date', readonly=True, copy=False)
    x_disb_reference = fields.Char(string='Transfer Reference', readonly=True, copy=False)
    x_disb_handed_date = fields.Date(
        string='Paid On', readonly=True, copy=False,
        help='Transfer value date, or the day the cheque or the cash was handed over.')
    x_disb_handed_by_id = fields.Many2one('res.users', string='Paid By', readonly=True, copy=False)
    x_disb_cashed_date = fields.Date(string='Cheque Cashed On', readonly=True, copy=False)
    x_disb_cashed_by_id = fields.Many2one('res.users', string='Cashing Recorded By', readonly=True, copy=False)
    x_disb_move_id = fields.Many2one('account.move', string='Payment Entry', readonly=True, copy=False)
    x_disb_cashed_move_id = fields.Many2one('account.move', string='Cheque Cashed Entry', readonly=True,
                                            copy=False)
    x_disb_error = fields.Text(string='Payment Posting Problem', readonly=True, copy=False)

    @api.depends('state', 'x_leave_id', 'x_is_revision', 'x_is_vacation_preview', 'x_net_wage',
                 'x_disb_method', 'x_disb_handed_date', 'x_disb_cashed_date',
                 'x_offcycle_bank_file_id', 'x_paid_outside_bank_file')
    def _compute_disb_state(self):
        for slip in self:
            if (slip.state != 'done' or not slip.x_leave_id or slip.x_is_revision
                    or slip.x_is_vacation_preview or slip.x_net_wage <= 0):
                slip.x_disb_state = 'none'
            elif slip.x_disb_method == 'cheque' and slip.x_disb_handed_date:
                slip.x_disb_state = 'paid' if slip.x_disb_cashed_date else 'cheque_handed'
            elif slip.x_disb_method == 'cash':
                slip.x_disb_state = 'paid' if slip.x_disb_handed_date else 'cash_pending'
            elif slip.x_disb_method == 'transfer' and slip.x_disb_handed_date:
                slip.x_disb_state = 'paid'
            elif slip.x_offcycle_bank_file_id or slip.x_paid_outside_bank_file:
                slip.x_disb_state = 'paid'
            else:
                slip.x_disb_state = 'to_choose'

    @api.model
    def _ksw_offcycle_pending_domain(self):
        # A cheque or the cash box is paying it: no bank file may as well.
        return super()._ksw_offcycle_pending_domain() + [
            ('x_disb_method', 'not in', ['transfer', 'cheque', 'cash'])]

    # ------------------------------------------------------------------ entries
    def _ksw_disb_payable(self):
        """The salaries payable the settlement entry credited: his payer's."""
        self.ensure_one()
        run = self._ksw_ledger_run()
        return run._ksw_ledger_payer(self).payable_account_id

    def _ksw_disb_paying_account(self):
        """Where a bank transfer of his comes out of, in the books."""
        self.ensure_one()
        bank = self._ksw_ledger_run()._resolve_slip_bank_account(self).sudo()
        if not bank:
            raise UserError(_('%(name)s has no paying bank account.', name=self.employee_id.name))
        if not bank.x_ledger_journal_id:
            raise UserError(_('The paying account %(bank)s names no bank journal (Paid From) in '
                              'Accounting.', bank=bank.display_name))
        return bank.x_ledger_journal_id

    def _ksw_disb_entry(self, journal, date, debit, credit, what):
        """One payment entry for this settlement: Dr ``debit`` / Cr ``credit``."""
        self.ensure_one()
        Ledger = self.env['ksw.payroll.ledger']
        Journal = self.env['ksw.bas.journal']
        employee = self.employee_id.sudo()
        amount = self.x_net_wage
        label = Journal.ref(what, Journal.employee_label(employee),
                            Journal.month_label(self.date_to))
        who = self.env['account.move.line']._ksw_employee_vals(employee)
        return Ledger._move(journal, date, '%s · %s' % (what, self.number or self.name), [
            (debit, amount, label, None, who), (credit, -amount, label, None, who)])

    def _ksw_disb_post(self):
        """Every payment entry this settlement should have and has not: the
        payment itself and, for a cashed cheque, the bank's. A problem is
        written on the payslip and never undoes the payment it records."""
        lang = self.env['ksw.bas.journal'].voucher_lang()
        if lang and self.env.context.get('lang') != lang:
            return self.with_context(lang=lang)._ksw_disb_post()
        Ledger = self.env['ksw.payroll.ledger']
        for slip in self.sudo():
            if slip.x_disb_state not in ('paid', 'cheque_handed') or not slip.x_disb_method:
                continue
            error = False
            try:
                with self.env.cr.savepoint():
                    payable = slip._ksw_disb_payable()
                    if not payable:
                        raise UserError(_('no salaries payable account for his paying entity'))
                    if not slip.x_disb_move_id and Ledger._applies(slip.x_disb_handed_date):
                        if slip.x_disb_method == 'bank':
                            journal = slip._ksw_disb_paying_account()
                            credit, what = journal._ksw_ledger_account(), _('Settlement bank transfer')
                        elif slip.x_disb_method == 'transfer':
                            journal = slip.x_disb_journal_id
                            credit = journal._ksw_ledger_account()
                            what = _('Settlement transfer %(ref)s', ref=slip.x_disb_reference or '')
                        elif slip.x_disb_method == 'cheque':
                            journal = slip.x_disb_journal_id
                            credit = journal._ksw_cheques_account()
                            what = _('Settlement cheque %(no)s', no=slip.x_disb_cheque_no or '')
                        else:
                            journal = slip.x_disb_journal_id
                            credit, what = journal._ksw_ledger_account(), _('Settlement paid in cash')
                        slip.x_disb_move_id = slip._ksw_disb_entry(
                            journal, slip.x_disb_handed_date, payable, credit, what)
                    if (slip.x_disb_method == 'cheque' and slip.x_disb_cashed_date
                            and not slip.x_disb_cashed_move_id
                            and Ledger._applies(slip.x_disb_cashed_date)):
                        journal = slip.x_disb_journal_id
                        slip.x_disb_cashed_move_id = slip._ksw_disb_entry(
                            journal, slip.x_disb_cashed_date, journal._ksw_cheques_account(),
                            journal._ksw_ledger_account(),
                            _('Settlement cheque %(no)s cashed', no=slip.x_disb_cheque_no or ''))
            except (UserError, ValidationError) as e:
                error = str(e.args[0] if e.args else e)
            if error != (slip.x_disb_error or False):
                slip.x_disb_error = error
                if error:
                    Ledger._note(slip.x_leave_id, Markup('<b>%(t)s</b><br/>%(m)s') % {
                        't': _('The settlement payment was not posted to the ledger'), 'm': error})


class KswRevisionBankFile(models.Model):
    _inherit = 'ksw.revision.bank.file'

    def action_generate(self):
        """Whichever way the file was made (from the request, or the menu),
        each settlement in it is now paid by bank transfer."""
        res = super().action_generate()
        for rec in self.sudo():
            slips = rec.leave_line_ids.payslip_id
            for slip in slips:
                vals = {'x_disb_handed_date': rec.value_date,
                        'x_disb_handed_by_id': rec.generated_by_id.id or self.env.uid}
                if not slip.x_disb_method:
                    vals['x_disb_method'] = 'bank'
                slip.write(vals)
            slips._ksw_disb_post()
        return res


class HrLeave(models.Model):
    _inherit = 'hr.leave'

    # Stored: the Home inbox and the filters search on it. No groups= on any
    # of these: they are read in invisible= by accounting, HR and the
    # disbursement officer (pitfall #31); the computes read through sudo.
    x_disb_state = fields.Selection(
        STATES, string='Settlement Payment', compute='_compute_disb_state', store=True,
        compute_sudo=True)
    x_disb_amount = fields.Float(string='Amount to Pay', compute='_compute_disb_view',
                                 compute_sudo=True, digits=(16, 2))
    x_disb_method = fields.Selection(METHODS, string='Payment Method', compute='_compute_disb_view',
                                     compute_sudo=True)
    x_disb_journal_name = fields.Char(string='Paid From', compute='_compute_disb_view', compute_sudo=True)
    x_disb_cheque_no = fields.Char(string='Cheque No.', compute='_compute_disb_view', compute_sudo=True)
    x_disb_cheque_date = fields.Date(string='Cheque Date', compute='_compute_disb_view', compute_sudo=True)
    x_disb_reference = fields.Char(string='Transfer Reference', compute='_compute_disb_view', compute_sudo=True)
    x_disb_handed_date = fields.Date(string='Paid On', compute='_compute_disb_view', compute_sudo=True)
    x_disb_handed_by = fields.Char(string='Paid By', compute='_compute_disb_view', compute_sudo=True)
    x_disb_cashed_date = fields.Date(string='Cheque Cashed On', compute='_compute_disb_view',
                                     compute_sudo=True)
    x_disb_bank_file_id = fields.Many2one('ksw.revision.bank.file', string='Bank File',
                                          compute='_compute_disb_view', compute_sudo=True)
    # The reference is for everyone who sees the payment; the file itself
    # (his account number and amount) only for those who may open bank files.
    x_disb_bank_file_ref = fields.Char(string='Bank File Ref.', compute='_compute_disb_view',
                                       compute_sudo=True)
    x_disb_bank_file_attachment_ids = fields.Many2many(
        'ir.attachment', string='Bank File (Excel / TXT)', compute='_compute_disb_view',
        compute_sudo=True)
    x_disb_entries = fields.Char(string='Journal Entries', compute='_compute_disb_view', compute_sudo=True)
    x_disb_error = fields.Text(string='Ledger Problem', compute='_compute_disb_view', compute_sudo=True)
    x_can_disb_pay = fields.Boolean(compute='_compute_disb_permissions')
    x_can_disb_cash = fields.Boolean(compute='_compute_disb_permissions')
    x_can_disb_cashed = fields.Boolean(compute='_compute_disb_permissions')
    x_can_disb_post = fields.Boolean(compute='_compute_disb_permissions')

    def _ksw_settlement_slips(self):
        self.ensure_one()
        return self.sudo().x_vacation_payslip_ids.filtered(
            lambda s: s.state == 'done' and s._ksw_is_settlement())

    def _ksw_disb_slip(self):
        """The settlement the leave is about now: the one waiting, else the last."""
        slips = self._ksw_settlement_slips()
        for state in STATE_PRIORITY:
            found = slips.filtered(lambda s: s.x_disb_state == state)
            if found:
                return found.sorted('id')[-1]
        return slips.browse()

    @api.depends('x_vacation_payslip_ids.x_disb_state')
    def _compute_disb_state(self):
        for leave in self:
            states = set(leave._ksw_settlement_slips().mapped('x_disb_state'))
            leave.x_disb_state = next((s for s in STATE_PRIORITY if s in states), 'none')

    @api.depends('x_disb_state', 'x_vacation_payslip_ids.x_disb_move_id',
                 'x_vacation_payslip_ids.x_ledger_move_id', 'x_vacation_payslip_ids.x_disb_error',
                 'x_vacation_payslip_ids.x_ledger_error')
    def _compute_disb_view(self):
        for leave in self:
            slip = leave._ksw_disb_slip()
            leave.x_disb_amount = slip.x_net_wage if slip else 0.0
            leave.x_disb_method = slip.x_disb_method or (
                'bank' if slip.x_offcycle_bank_file_id else False)
            leave.x_disb_journal_name = slip.x_disb_journal_id.display_name or False
            leave.x_disb_cheque_no = slip.x_disb_cheque_no
            leave.x_disb_cheque_date = slip.x_disb_cheque_date
            leave.x_disb_reference = slip.x_disb_reference
            leave.x_disb_handed_date = slip.x_disb_handed_date
            leave.x_disb_handed_by = slip.x_disb_handed_by_id.name or False
            leave.x_disb_cashed_date = slip.x_disb_cashed_date
            leave.x_disb_bank_file_id = slip.x_offcycle_bank_file_id
            leave.x_disb_bank_file_ref = slip.x_offcycle_bank_file_id.name or False
            leave.x_disb_bank_file_attachment_ids = slip.x_offcycle_bank_file_id.attachment_ids
            leave.x_disb_entries = ', '.join(filter(None, (
                slip.x_ledger_move_id.name, slip.x_disb_move_id.name,
                slip.x_disb_cashed_move_id.name))) or False
            leave.x_disb_error = '\n'.join(filter(None, (
                slip.x_ledger_error, slip.x_disb_error))) or False

    def _ksw_is_disb_accountant(self, user=None):
        """The request's own accountants (those of its accounting step), or an
        accounting administrator."""
        self.ensure_one()
        user = user or self.env.user
        return user.has_group(ACC_MANAGER) or user in self._department_accountant_users(self)

    @api.depends_context('uid')
    @api.depends('x_disb_state')
    def _compute_disb_permissions(self):
        user = self.env.user
        disb = bool(self.env.ref(DISB_GROUP, raise_if_not_found=False)) and user.has_group(DISB_GROUP)
        for leave in self:
            acc = bool(leave.id) and leave.x_disb_state != 'none' and leave._ksw_is_disb_accountant(user)
            leave.x_can_disb_pay = acc and leave.x_disb_state == 'to_choose'
            leave.x_can_disb_cash = disb and leave.x_disb_state == 'cash_pending'
            leave.x_can_disb_cashed = acc and leave.x_disb_state == 'cheque_handed'
            leave.x_can_disb_post = acc and bool(leave.x_disb_error)

    # ------------------------------------------------------------------ Waiting for you
    @api.depends_context('uid')
    def _compute_is_pending_my_action(self):
        super()._compute_is_pending_my_action()
        user = self.env.user
        disb = bool(self.env.ref(DISB_GROUP, raise_if_not_found=False)) and user.has_group(DISB_GROUP)
        for leave in self:
            if leave.x_is_pending_my_action or not leave.id:
                continue
            if leave.x_disb_state == 'to_choose':
                leave.x_is_pending_my_action = user in self._department_accountant_users(leave)
            elif leave.x_disb_state == 'cash_pending':
                leave.x_is_pending_my_action = disb

    @api.model
    def _ksw_disb_pending_domain(self):
        """The payment steps waiting for the current user: choosing how to pay
        (the request's accountants), handing out the cash (the officer)."""
        uid = self.env.uid
        parts = [
            [('x_disb_state', '=', 'to_choose'),
             ('employee_id.department_id.x_effective_accountant_ids.user_id', '=', uid)],
            [('x_disb_state', '=', 'to_choose'), ('employee_id.department_id', '=', False),
             ('company_id.x_default_accountant_ids.user_id', '=', uid)],
        ]
        if self.env.ref(DISB_GROUP, raise_if_not_found=False) and self.env.user.has_group(DISB_GROUP):
            parts.append([('x_disb_state', '=', 'cash_pending')])
        return odoo_expr.OR(parts)

    def _search_is_pending_my_action(self, operator, value):
        # Odoo 19 hands a search= method 'in'/'not in' with a set (pitfall #24).
        if operator in ('in', 'not in'):
            positive_wanted = (operator == 'in') == any(value)
        elif operator in ('=', '!='):
            positive_wanted = (operator == '=') == bool(value)
        else:
            return NotImplemented
        positive = odoo_expr.OR([super()._search_is_pending_my_action('in', [True]),
                                 self._ksw_disb_pending_domain()])
        if positive_wanted:
            return positive
        return [('id', 'not in', self.with_context(active_test=False).search(positive).ids)]

    # ------------------------------------------------------------------ status line
    @api.depends('x_vacation_payslip_ids.state',
                 'x_vacation_payslip_ids.x_offcycle_bank_file_id',
                 'x_vacation_payslip_ids.x_paid_outside_bank_file',
                 'x_vacation_payslip_ids.x_disb_state')
    def _compute_settlement_transfer_status(self):
        super()._compute_settlement_transfer_status()
        for leave in self:
            parts = []
            for slip in leave._ksw_settlement_slips():
                ref = slip.number or slip.name
                amount = '{:,.2f}'.format(slip.x_net_wage)
                if slip.x_disb_method == 'cheque' and slip.x_disb_handed_date:
                    parts.append(_(
                        '%(slip)s: cheque %(no)s on %(bank)s for %(amt)s SAR handed over on %(date)s, '
                        '%(cashed)s', slip=ref, no=slip.x_disb_cheque_no or '',
                        bank=slip.x_disb_journal_id.display_name or '', amt=amount,
                        date=slip.x_disb_handed_date,
                        cashed=_('cashed on %s') % slip.x_disb_cashed_date
                        if slip.x_disb_cashed_date else _('not cashed yet')))
                elif slip.x_disb_method == 'transfer' and slip.x_disb_handed_date:
                    parts.append(_(
                        '%(slip)s: %(amt)s SAR transferred from %(bank)s on %(date)s (ref. %(ref)s)',
                        slip=ref, amt=amount, bank=slip.x_disb_journal_id.display_name or '',
                        date=slip.x_disb_handed_date, ref=slip.x_disb_reference or ''))
                elif slip.x_disb_method == 'cash':
                    parts.append(_(
                        '%(slip)s: %(amt)s SAR in cash from %(box)s, %(done)s', slip=ref, amt=amount,
                        box=slip.x_disb_journal_id.display_name or '',
                        done=_('handed over on %s') % slip.x_disb_handed_date
                        if slip.x_disb_handed_date else _('waiting for the disbursement officer')))
                elif slip.x_disb_state == 'to_choose':
                    parts.append(_('%(slip)s: %(amt)s SAR to pay, waiting for accounting to choose '
                                   'bank file, bank transfer, cheque or cash', slip=ref, amt=amount))
            if parts:
                leave.x_settlement_transfer_status = '\n'.join(parts)

    # ------------------------------------------------------------------ actions
    def _ksw_disb_check_accountant(self):
        self.ensure_one()
        if not self.env.su and not self._ksw_is_disb_accountant():
            raise UserError(_("Only this request's accountants (its accounting step) may pay "
                              'the settlement.'))

    def action_disb_pay(self):
        self.ensure_one()
        self._ksw_disb_check_accountant()
        if self.x_disb_state != 'to_choose':
            raise UserError(_('This settlement is not waiting to be paid.'))
        return self._ksw_disb_wizard('pay')

    def action_disb_cheque_cashed(self):
        self.ensure_one()
        self._ksw_disb_check_accountant()
        if self.x_disb_state != 'cheque_handed':
            raise UserError(_('No cheque of this settlement is waiting to be cashed.'))
        return self._ksw_disb_wizard('cashed')

    def _ksw_disb_wizard(self, mode):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Pay Settlement') if mode == 'pay' else _('Cheque Cashed'),
            'res_model': 'ksw.settlement.payment.wizard',
            'view_mode': 'form', 'target': 'new',
            'context': {'default_leave_id': self.id, 'default_mode': mode},
        }

    def action_open_disb_bank_file(self):
        """The off-cycle bank file that paid this settlement, its files to download."""
        self.ensure_one()
        bank_file = self.x_disb_bank_file_id
        if not bank_file:
            raise UserError(_('No bank file has paid this settlement.'))
        # Opened as the user: the bank file's own access rights decide.
        bank_file.with_env(self.env).check_access('read')
        return {
            'type': 'ir.actions.act_window', 'res_model': 'ksw.revision.bank.file',
            'res_id': bank_file.id, 'view_mode': 'form', 'target': 'current',
        }

    def action_disb_cash_handed(self):
        """The disbursement officer: the cash is in the employee's hand."""
        self.ensure_one()
        if not self.env.su and not (self.env.ref(DISB_GROUP, raise_if_not_found=False)
                                    and self.env.user.has_group(DISB_GROUP)):
            raise UserError(_('Only a Loan Disbursement Officer may confirm a cash handover.'))
        slips = self._ksw_settlement_slips().filtered(lambda s: s.x_disb_state == 'cash_pending')
        if not slips:
            raise UserError(_('No cash payment of this settlement is waiting for handover.'))
        today = fields.Date.context_today(self)
        # The money has left: mark it, so neither a bank file nor a leave
        # reversal can pay or release it again.
        slips.write({'x_disb_handed_date': today, 'x_disb_handed_by_id': self.env.uid,
                     'x_paid_outside_bank_file': True})
        slips._ksw_disb_post()
        self._ksw_disb_note(_('💵 Cash handed over'), slips, Markup(
            '<b>%(l1)s</b> %(box)s<br/>') % {'l1': _('From:'), 'box': slips[:1].x_disb_journal_id.display_name})
        return True

    def action_disb_post_ledger(self):
        """Retry what could not be posted (a missing account fixed since)."""
        self.ensure_one()
        self._ksw_disb_check_accountant()
        slips = self._ksw_settlement_slips()
        slips._ksw_ledger_post_settlement()
        slips._ksw_disb_post()
        return True

    def _ksw_disb_note(self, title, slips, extra=Markup(''), partners=None):
        body = Markup('<strong>%(t)s</strong><br/><b>%(l1)s</b> %(user)s<br/>'
                      '<b>%(l2)s</b> %(amt)s SAR<br/>') % {
            't': title, 'l1': _('By:'), 'user': self.env.user.name, 'l2': _('Amount:'),
            'amt': '{:,.2f}'.format(sum(slips.mapped('x_net_wage')))} + extra
        self.sudo().message_post(
            body=body, subtype_xmlid='mail.mt_comment' if partners else 'mail.mt_note',
            partner_ids=partners.ids if partners else [])


class KswSettlementPaymentWizard(models.TransientModel):
    _name = 'ksw.settlement.payment.wizard'
    _description = 'Pay a Leave Settlement'

    leave_id = fields.Many2one('hr.leave', required=True, ondelete='cascade')
    mode = fields.Selection([('pay', 'Pay'), ('cashed', 'Cheque Cashed')], default='pay', required=True)
    employee_name = fields.Char(string='Employee', compute='_compute_summary')
    amount = fields.Float(string='Amount', compute='_compute_summary', digits=(16, 2))
    paying_bank = fields.Char(string='Paying Account', compute='_compute_summary')
    # Bank transfer: three different accounts, each shown for what it is.
    pay_from_id = fields.Many2one(
        'res.partner.bank', string='Pay From', domain="[('id', 'in', pay_from_ids)]",
        help="The company's paying channel the transfer leaves from; it decides the bank file "
             "format. Only the channels of the entity whose salaries payable the settlement "
             "was booked to.")
    pay_from_ids = fields.Many2many('res.partner.bank', compute='_compute_summary')
    pay_from_count = fields.Integer(compute='_compute_summary')
    pay_from_ledger = fields.Char(string='Credited in the Ledger', compute='_compute_pay_from_ledger')
    beneficiary = fields.Char(string='To (Beneficiary)', compute='_compute_summary',
                              help="The employee's own account the bank file pays into.")
    method = fields.Selection(METHODS, string='Pay By', default='bank')
    journal_type = fields.Char(compute='_compute_journal_type')
    journal_id = fields.Many2one(
        'account.journal', string='From', domain="[('type', '=', journal_type)]",
        help='The bank the transfer leaves from or the cheque is drawn on, or the cash box that pays.')
    cheque_no = fields.Char(string='Cheque No.')
    reference = fields.Char(string='Transfer Reference',
                            help="The bank's reference for the transfer, as on the online-banking receipt.")
    cheque_date = fields.Date(string='Cheque Date', default=fields.Date.context_today)
    date = fields.Date(string='Date', required=True, default=fields.Date.context_today,
                       help='Bank transfer: the value date. Cheque: the day it is handed over. '
                            'Cheque cashed: the day the bank paid it.')

    @api.depends('leave_id')
    def _compute_summary(self):
        for wiz in self:
            leave = wiz.leave_id.sudo()
            slips = leave._ksw_settlement_slips().filtered(
                lambda s: s.x_disb_state == ('to_choose' if wiz.mode == 'pay' else 'cheque_handed')
            ) if leave else self.env['hr.payslip']
            employee = leave.employee_id.sudo()
            wiz.employee_name = employee.name or False
            wiz.amount = sum(slips.mapped('x_net_wage'))
            bank = slips[:1] and slips[:1]._ksw_ledger_run()._resolve_slip_bank_account(slips[:1])
            wiz.paying_bank = bank.sudo().display_name if bank else False
            channels = wiz._pay_from_channels(bank)
            wiz.pay_from_ids = channels
            wiz.pay_from_count = len(channels)
            own = employee.primary_bank_account_id
            wiz.beneficiary = ' · '.join(filter(None, (
                own.bank_id.name, own.acc_number, own.partner_id.name))) if own else False

    def _pay_from_channels(self, bank):
        """The company accounts a transfer may leave from: the employee's own
        channel and the others of the same entity that have a bank journal.
        Another entity's would pay a payable it was not booked to."""
        bank = bank.sudo() if bank else self.env['res.partner.bank']
        if not bank:
            return bank
        company = self.leave_id.sudo().company_id or self.env.company
        entity = self.env['ksw.payroll.posting']._payer_entity(bank, company)
        Bank = self.env['res.partner.bank'].sudo()
        same = Bank.search([('partner_id', '=', bank.partner_id.id), ('x_ledger_journal_id', '!=', False)])
        same = same.filtered(lambda b: self.env['ksw.payroll.posting']._payer_entity(b, company) == entity)
        return (bank | same).with_env(self.env)

    @api.onchange('leave_id', 'mode')
    def _onchange_default_pay_from(self):
        for wiz in self:
            if not wiz.pay_from_id:
                leave = wiz.leave_id.sudo()
                slip = leave._ksw_settlement_slips().filtered(lambda s: s.x_disb_state == 'to_choose')[:1] \
                    if leave else None
                bank = slip and slip._ksw_ledger_run()._resolve_slip_bank_account(slip)
                wiz.pay_from_id = bank.id if bank else False

    @api.depends('pay_from_id')
    def _compute_pay_from_ledger(self):
        for wiz in self:
            journal = wiz.pay_from_id.sudo().x_ledger_journal_id
            account = journal._ksw_ledger_account() if journal else None
            wiz.pay_from_ledger = '%s · %s %s' % (journal.code, account.code, account.name) \
                if account else False

    @api.depends('method')
    def _compute_journal_type(self):
        for wiz in self:
            wiz.journal_type = 'cash' if wiz.method == 'cash' else 'bank'

    def _slips(self, state):
        return self.leave_id._ksw_settlement_slips().filtered(lambda s: s.x_disb_state == state)

    def action_confirm(self):
        self.ensure_one()
        leave = self.leave_id
        leave._ksw_disb_check_accountant()
        if self.mode == 'cashed':
            return self._confirm_cashed(leave)
        slips = self._slips('to_choose')
        if not slips:
            raise UserError(_('This settlement is not waiting to be paid.'))
        if self.method == 'bank':
            return self._pay_by_bank(leave, slips)
        if not self.journal_id or self.journal_id.type != self.journal_type:
            raise UserError(_('Choose the cash box that pays.') if self.method == 'cash'
                            else _('Choose the bank the money leaves from.'))
        if self.method == 'transfer':
            return self._pay_by_transfer(leave, slips)
        if self.method == 'cheque':
            if not (self.cheque_no or '').strip():
                raise UserError(_('Enter the cheque number.'))
            # Handed over is paid: the employee holds it, so no bank file and
            # no leave reversal may pay or release it again.
            slips.write({
                'x_disb_method': 'cheque', 'x_disb_journal_id': self.journal_id.id,
                'x_disb_cheque_no': self.cheque_no.strip(), 'x_disb_cheque_date': self.cheque_date,
                'x_disb_handed_date': self.date, 'x_disb_handed_by_id': self.env.uid,
                'x_paid_outside_bank_file': True,
            })
            slips._ksw_disb_post()
            leave._ksw_disb_note(_('🧾 Settlement paid by cheque'), slips, Markup(
                '<b>%(l1)s</b> %(no)s · %(bank)s<br/><b>%(l2)s</b> %(date)s<br/>') % {
                    'l1': _('Cheque:'), 'no': self.cheque_no, 'bank': self.journal_id.display_name,
                    'l2': _('Handed over on:'), 'date': self.date})
            return True
        slips.write({'x_disb_method': 'cash', 'x_disb_journal_id': self.journal_id.id})
        officers = self.env.ref(DISB_GROUP).sudo().user_ids.partner_id
        leave._ksw_disb_note(_('💵 Settlement to be paid in cash'), slips, Markup(
            '<b>%(l1)s</b> %(box)s<br/>%(l2)s<br/>') % {
                'l1': _('From:'), 'box': self.journal_id.display_name,
                'l2': _('Waiting for the Loan Disbursement Officer to hand it over.')},
            partners=officers)
        return True

    def _pay_by_bank(self, leave, slips):
        """A bank file for him alone, generated now and kept on the request."""
        if not self.beneficiary:
            raise UserError(_('%(name)s has no bank account of his own to transfer to. Add it on '
                              'his employee record, or pay by cheque or cash.', name=self.employee_name))
        channel = self.pay_from_id.sudo()
        if not channel or channel not in self._pay_from_channels(channel).sudo():
            raise UserError(_('Choose the company account the transfer leaves from.'))
        if not channel.x_ledger_journal_id:
            raise UserError(_('The paying account %(bank)s names no bank journal (Paid From) in '
                              'Accounting.', bank=channel.display_name))
        default = slips[:1]._ksw_ledger_run()._resolve_slip_bank_account(slips[:1]).sudo()
        if channel != default:
            if channel not in self._pay_from_channels(default).sudo():
                raise UserError(_('%(bank)s belongs to another entity than the one this settlement '
                                  'was booked to.', bank=channel.display_name))
            # The stamp is the account the money really left from: the bank
            # file groups by it, and so does the payment entry.
            slips.sudo().write({'x_paid_bank_account_id': channel.id})
        # sudo after the accountant check: an accounting administrator need
        # not also hold the leave-accountant group the file model asks for.
        File = self.env['ksw.revision.bank.file'].sudo()
        bank_file = File.create({
            'value_date': self.date,
            'request_ids': [(6, 0, [])],
            'leave_line_ids': [(0, 0, {'payslip_id': s.id}) for s in slips],
        })
        slips.sudo().write({'x_disb_method': 'bank'})
        bank_file.action_generate()
        leave._ksw_disb_note(_('🏦 Settlement paid by bank transfer'), slips, Markup(
            '<b>%(l1)s</b> %(file)s<br/><b>%(l2)s</b> %(date)s<br/>') % {
                'l1': _('Bank file:'), 'file': bank_file.name, 'l2': _('Value date:'), 'date': self.date})
        return True

    def _pay_by_transfer(self, leave, slips):
        """Transferred by hand from any bank's online banking: no file."""
        if not self.beneficiary:
            raise UserError(_('%(name)s has no bank account of his own to transfer to. Add it on '
                              'his employee record, or pay by cheque or cash.', name=self.employee_name))
        if not (self.reference or '').strip():
            raise UserError(_("Enter the bank's transfer reference."))
        # Sent is paid: no payroll bank file may pay it again, and no leave
        # reversal may release it.
        slips.write({
            'x_disb_method': 'transfer', 'x_disb_journal_id': self.journal_id.id,
            'x_disb_reference': self.reference.strip(),
            'x_disb_handed_date': self.date, 'x_disb_handed_by_id': self.env.uid,
            'x_paid_outside_bank_file': True,
        })
        slips._ksw_disb_post()
        leave._ksw_disb_note(_('🏦 Settlement paid by bank transfer'), slips, Markup(
            '<b>%(l1)s</b> %(bank)s<br/><b>%(l2)s</b> %(ref)s<br/><b>%(l3)s</b> %(date)s<br/>') % {
                'l1': _('From:'), 'bank': self.journal_id.display_name,
                'l2': _('Reference:'), 'ref': self.reference.strip(),
                'l3': _('Value date:'), 'date': self.date})
        return True

    def _confirm_cashed(self, leave):
        slips = self._slips('cheque_handed')
        if not slips:
            raise UserError(_('No cheque of this settlement is waiting to be cashed.'))
        if any(self.date < s.x_disb_handed_date for s in slips):
            raise UserError(_('A cheque cannot be cashed before it was handed over.'))
        slips.write({'x_disb_cashed_date': self.date, 'x_disb_cashed_by_id': self.env.uid})
        slips._ksw_disb_post()
        leave._ksw_disb_note(_('✅ Settlement cheque cashed'), slips, Markup(
            '<b>%(l1)s</b> %(date)s<br/>') % {'l1': _('Cashed on:'), 'date': self.date})
        return True


class KswPayrollLedger(models.AbstractModel):
    _inherit = 'ksw.payroll.ledger'

    @api.model
    def _rebook_settlements(self, dry_run=True):
        """Bring the settlements confirmed before Nov 2026 into the ledger.

        Booked the old way (in the off-cycle batch, dated its period end, one
        wage line): cancelled and booked again. Never booked: booked. Sent in
        an off-cycle bank file: its transfer posted, at the file's value date.
        Transferred by hand: left without a payment entry and listed, since
        nothing says whether it was a transfer, a cheque or cash.
        Returns what it did (or would do, ``dry_run``)."""
        Slip = self.env['hr.payslip'].sudo()
        start = self._start_date()
        slips = Slip.search([('state', '=', 'done'), ('x_leave_id', '!=', False),
                             ('x_is_revision', '=', False), ('x_is_vacation_preview', '=', False)],
                            order='id')
        report = {'rebooked': [], 'booked': [], 'bank_paid': [], 'by_hand': [], 'problems': []}
        for slip in slips:
            date = slip._ksw_settlement_date()
            if not start or date < start:
                continue
            old = slip.x_ledger_move_id
            if old and slip.x_ledger_settlement:
                pass
            elif old:
                report['rebooked'].append('%s %s (%s)' % (slip.number, slip.employee_id.name, old.name))
                if not dry_run:
                    # The old entry is the whole off-cycle batch's: whatever
                    # else it carried (a salary revision) is booked again the
                    # way it was, through its batch.
                    sharing = Slip.search([('x_ledger_move_id', '=', old.id)])
                    self._cancel(old)
                    sharing.write({'x_ledger_move_id': False})
                    for run in (sharing - slip).filtered(lambda s: not s._ksw_is_settlement()).payslip_run_id:
                        run._ksw_ledger_post()
            else:
                report['booked'].append('%s %s' % (slip.number, slip.employee_id.name))
            if not dry_run:
                slip._ksw_ledger_post_settlement()
                if slip.x_ledger_error:
                    report['problems'].append('%s: %s' % (slip.number, slip.x_ledger_error))
            bank_file = slip.x_offcycle_bank_file_id
            if bank_file and not slip.x_disb_move_id:
                report['bank_paid'].append('%s %s (%s)' % (slip.number, slip.employee_id.name, bank_file.name))
                if not dry_run:
                    slip.write({'x_disb_method': slip.x_disb_method or 'bank',
                                'x_disb_handed_date': slip.x_disb_handed_date or bank_file.value_date,
                                'x_disb_handed_by_id': slip.x_disb_handed_by_id.id or bank_file.generated_by_id.id})
                    slip._ksw_disb_post()
                    if slip.x_disb_error:
                        report['problems'].append('%s: %s' % (slip.number, slip.x_disb_error))
            elif slip.x_paid_outside_bank_file and not slip.x_disb_method:
                report['by_hand'].append('%s %s' % (slip.number, slip.employee_id.name))
        return report
