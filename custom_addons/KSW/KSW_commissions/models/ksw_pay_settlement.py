"""BAS vouchers for a commission month — every file posts the difference.

A commission month reaches BAS in stages, weeks apart: the right is proved
first (Dr expense / Cr commissions accrual), deductions are taken out of it
while it waits (Dr accrual / Cr his advance), and the transfer settles it
(Dr accrual / Cr the bank). Between those stages the month itself moves —
reopened and approved again, entries corrected, a man paid on his vacation
payslip instead.

So nothing here exports "the month". Every BAS file is a voucher holding
items, and every item is one fact keyed for life:

* ``accrual:<run>:<employee>:<expense>|<accrual>|<label>`` — his right,
  per pay type and accounts;
* ``deduction:<installment>`` — a deduction taken from it, whenever charged;
* ``unlinked:<register line>`` — a legacy offset with no installment;
* ``payment:<register line>`` — the transfer;
* ``bas:<voucher>:<serial>`` — a line of a BAS voucher loaded as the
  starting point that matches nothing in Odoo (so the next file reverses it).

What BAS holds is the sum of the items of every voucher; what it should hold
is what the month says now. The next file — the month's journal export, a
settlement voucher, or Mark Paid's final settlement — posts the difference,
and the earlier files stay as the month's history. A month that has been
reopened is frozen: nothing of it is reversed while it is being corrected;
the difference comes out once it is approved again.
"""
from collections import defaultdict

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

ACCOUNTANT = 'KSW_commissions.group_commission_accountant'
# Only an approved month has a register whose lines can be paid or settle a
# deduction; a paid one is still the source of its posted payments.
SETTLED_RUN_STATES = ('approved', 'paid')


class KswPaySettlement(models.Model):
    _name = 'ksw.pay.settlement'
    _description = 'Commission Settlement Voucher'
    _inherit = ['mail.thread']
    _order = 'id desc'

    name = fields.Char(
        string='Reference', required=True, copy=False, readonly=True,
        default=lambda self: _('New'), index=True,
    )
    state = fields.Selection(
        [('draft', 'Draft'), ('done', 'Posted to BAS'),
         ('cancel', 'Cancelled: Not Imported')],
        string='Status', default='draft', required=True, readonly=True,
        copy=False, tracking=True,
    )
    company_id = fields.Many2one(
        'res.company', readonly=True, default=lambda self: self.env.company,
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id', readonly=True,
    )
    date = fields.Date(
        string='Voucher Date', required=True,
        default=fields.Date.context_today,
        help='The date of the BAS voucher (the "fdate" column). Each line '
             'still names the month and the event it settles.',
    )
    journal_number = fields.Char(
        string='Journal Number',
        help='The voucher number reserved in BAS, written in the "number" '
             'column of every line.',
    )
    item_ids = fields.One2many(
        'ksw.pay.settlement.item', 'settlement_id', string='Transactions',
        copy=False,
        help='Every payment and later deduction not yet posted to BAS when '
             'the voucher was collected. Remove a line to leave it for the '
             'next voucher.',
    )
    item_count = fields.Integer(compute='_compute_totals')
    total_paid = fields.Monetary(
        string='Paid by Bank', compute='_compute_totals')
    total_deducted = fields.Monetary(
        string='Deducted', compute='_compute_totals')
    attachment_id = fields.Many2one(
        'ir.attachment', string='BAS File', readonly=True, copy=False)
    posted_manually = fields.Boolean(
        string='Recorded Without File', readonly=True, copy=False,
        help='Marked as already in BAS without generating a file — used for '
             'transactions the accountant had already entered by hand.',
    )
    done_by_id = fields.Many2one(
        'res.users', string='Posted By', readonly=True, copy=False)
    done_date = fields.Datetime(string='Posted On', readonly=True, copy=False)
    note = fields.Text()
    origin = fields.Selection(
        [('month', 'Journal Export'),
         ('voucher', 'Settlement Voucher'),
         ('mark_paid', 'Mark Paid (Final Settlement)'),
         ('baseline', 'Loaded from BAS')],
        string='Made By', default='voucher', required=True, readonly=True,
        copy=False,
        help='Journal Export: the month\'s Export ▸ Journal entries — the '
             'first one posts the month, each later one only what changed. '
             'Mark Paid: the final settlement, posted when the transfer is '
             'recorded. Settlement Voucher: prepared by the accountant across '
             'months. Loaded from BAS: a voucher already in BAS, recorded as '
             'the starting point.')
    run_id = fields.Many2one(
        'ksw.pay.run', string='Commission Month', readonly=True, copy=False,
        help='The month this voucher was made for (journal export, Mark Paid '
             'or a BAS voucher loaded for it).')
    bas_voucher_number = fields.Char(
        string='BAS Voucher Loaded', readonly=True, copy=False)
    total_right = fields.Monetary(
        string='Commission Right', compute='_compute_totals')

    @api.depends('item_ids.amount', 'item_ids.kind')
    def _compute_totals(self):
        for rec in self:
            items = rec.item_ids
            rec.item_count = len(items)
            rec.total_right = sum(
                items.filtered(lambda i: i.kind == 'accrual').mapped('amount'))
            rec.total_paid = sum(
                items.filtered(lambda i: i.kind == 'payment').mapped('amount'))
            rec.total_deducted = sum(
                items.filtered(lambda i: i.kind == 'deduction')
                .mapped('amount'))

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        self._check_accountant()
        # Every voucher holds its transactions, a draft included, so a new
        # one is only ever started knowingly: through New Settlement
        # Voucher, which shows the drafts and the last posted voucher first.
        if (not self.env.su
                and not self.env.context.get('ksw_settlement_new_confirmed')
                and not all(v.get('origin') in ('mark_paid', 'month', 'baseline')
                            for v in vals_list)
                and self.search_count([])):
            raise UserError(_(
                'Start a new settlement voucher with "New Settlement '
                'Voucher": it shows the drafts still holding transactions '
                'and the last voucher posted before you confirm.'))
        Seq = self.env['ir.sequence'].sudo()
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = Seq.next_by_code(self._name) or _('New')
        records = super().create(vals_list)
        # A new voucher starts from everything waiting, like a revision
        # bank file starts from every pending request.
        for rec in records:
            if rec.origin == 'voucher' and not rec.item_ids:
                rec._collect()
        return records

    def write(self, vals):
        if not self.env.su:
            self._check_accountant()
            if self.filtered(lambda r: r.state != 'draft') and (
                    set(vals) - {'note'}):
                raise UserError(_(
                    'A posted settlement voucher cannot be changed: it is '
                    'what BAS was told.'))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            self._check_accountant()
            if self.filtered(lambda r: r.state != 'draft'):
                raise UserError(_(
                    'A posted settlement voucher cannot be deleted: its '
                    'transactions are recorded as being in BAS through it.'))
        return super().unlink()

    @api.model
    def _check_accountant(self):
        if not self.env.su and not self.env.user.has_group(ACCOUNTANT):
            raise UserError(_(
                'Only the Commission Accountant may prepare a settlement '
                'voucher.'))

    # ------------------------------------------------------------------
    # What should be in BAS, and what already is
    # ------------------------------------------------------------------
    @api.model
    def _settled_runs(self, runs=None):
        Run = self.env['ksw.pay.run'].sudo()
        if runs is None:
            return Run.search([('state', 'in', SETTLED_RUN_STATES)])
        return runs.sudo().filtered(lambda r: r.state in SETTLED_RUN_STATES)

    @api.model
    def _desired_events(self, runs=None):
        """``{key: item values}`` — everything BAS should hold today for
        ``runs`` (every approved or paid month when None)."""
        # A key carries the pay type's wording, so it must not depend on who
        # is looking: always BAS's language, as the file itself is written.
        lang = self._voucher_lang()
        if lang and self.env.context.get('lang') != lang:
            return self.with_context(lang=lang)._desired_events(
                runs.with_context(lang=lang) if runs is not None else None)
        DedLine = self.env['ksw.deduction.line'].sudo()
        events = {}
        for run in self._settled_runs(runs):
            month_end = run._bas_journal_date()
            lines = run.line_ids
            line_of = {l.employee_id.id: l for l in lines}
            # The right — the register's own figures, one per pay type.
            for employee_id, by_component in run._bas_component_totals().items():
                employee = self.env['hr.employee'].sudo().browse(employee_id)
                for detail in run._bas_detail_lines(by_component):
                    if not round(detail['amount'], 2):
                        continue
                    comp = detail['component']
                    key = 'accrual:%d:%d:%s|%s|%s' % (
                        run.id, employee_id, comp.x_bas_expense_code or '',
                        comp.x_bas_accrual_code or '', detail['label'])
                    events[key] = {
                        'key': key, 'kind': 'accrual', 'run_id': run.id,
                        'run_line_id': line_of.get(employee_id, lines[:0]).id,
                        'employee_id': employee_id,
                        'label': detail['label'],
                        'expense_code': comp.x_bas_expense_code,
                        'expense_name': comp.x_bas_expense_name,
                        'accrual_code': comp.x_bas_accrual_code,
                        'accrual_name': comp.x_bas_accrual_name,
                        # The truck, never the driver's own cost centre.
                        'cost_center': (employee.x_bas_equipment_code or ''
                                        if comp.x_bas_use_cost_center else ''),
                        'event_date': month_end,
                        'amount': round(detail['amount'], 2),
                    }
            if not lines:
                continue
            # Every deduction taken out of it, whenever it was charged.
            settled = DedLine.search([
                ('x_paid_via_pay_run_line_id', 'in', lines.ids),
                ('state', '=', 'paid'),
            ])
            for ded_line in settled:
                charged = ded_line.deduction_id.x_charge_date
                key = 'deduction:%d' % ded_line.id
                events[key] = {
                    'key': key, 'kind': 'deduction', 'run_id': run.id,
                    'run_line_id': ded_line.x_paid_via_pay_run_line_id.id,
                    'deduction_line_id': ded_line.id,
                    'employee_id': ded_line.x_paid_via_pay_run_line_id
                                           .employee_id.id,
                    'label': ded_line.deduction_id.name or '',
                    'event_date': max(d for d in (month_end, charged) if d),
                    'amount': round(ded_line.amount or 0.0, 2),
                }
            for line in lines:
                linked = sum(settled.filtered(
                    lambda d, l=line: d.x_paid_via_pay_run_line_id == l
                ).mapped('amount'))
                unlinked = round((line.loan_offset or 0.0) - linked, 2)
                if unlinked > 0.0:
                    key = 'unlinked:%d' % line.id
                    events[key] = {
                        'key': key, 'kind': 'deduction', 'run_id': run.id,
                        'run_line_id': line.id,
                        'employee_id': line.employee_id.id,
                        'event_date': month_end, 'amount': unlinked,
                    }
            # The transfer.
            for line in lines.filtered('x_paid'):
                amount = round(line.net_payable or 0.0, 2)
                if amount <= 0.0:
                    continue
                paid_on = line.x_paid_date and fields.Datetime.context_timestamp(
                    self, line.x_paid_date).date()
                key = 'payment:%d' % line.id
                events[key] = {
                    'key': key, 'kind': 'payment', 'run_id': run.id,
                    'run_line_id': line.id,
                    'employee_id': line.employee_id.id,
                    'bank_account_id': (line.bank_account_id
                                        or run.x_salary_bank_account_id).id,
                    'event_date': paid_on, 'amount': amount,
                }
        return events

    def _booked(self):
        """``{key: (amount, last item)}`` over every voucher but this one.

        Other drafts count too: an item collected into one draft is not
        offered to a second, so two vouchers can never both post it.
        Deleting the draft gives it back.
        """
        # A file cancelled as never imported holds nothing in BAS.
        domain = [('state', '!=', 'cancel')]
        if self.ids:
            domain.append(('settlement_id', 'not in', self.ids))
        booked = {}
        for item in self.env['ksw.pay.settlement.item'].sudo().search(
                domain, order='id'):
            amount, _last = booked.get(item.key, (0.0, None))
            booked[item.key] = (round(amount + item.amount, 2), item)
        return booked

    # What a reversal copies from the last item posted under its key.
    _REVERSAL_FIELDS = ('kind', 'label', 'expense_code', 'expense_name',
                        'accrual_code', 'accrual_name', 'cost_center',
                        'counter_code', 'counter_name')

    @api.model
    def _voucher_lang(self):
        if 'ksw.bas.journal' not in self.env:
            return False
        return self.env['ksw.bas.journal'].voucher_lang()

    @api.model
    def _vacation_parts(self, runs):
        """``{key: amount}`` — the part of each pay type already paid on a
        vacation payslip (and settled in BAS by that payslip's own entry)."""
        lang = self._voucher_lang()
        if lang and self.env.context.get('lang') != lang:
            return self.with_context(lang=lang)._vacation_parts(
                runs.with_context(lang=lang))
        parts = defaultdict(float)
        for run in runs:
            for employee, (_entries, by_component) in (
                    run._vacation_settled_totals().items()):
                for detail in run._bas_detail_lines(by_component):
                    comp = detail['component']
                    key = 'accrual:%d:%d:%s|%s|%s' % (
                        run.id, employee.id, comp.x_bas_expense_code or '',
                        comp.x_bas_accrual_code or '', detail['label'])
                    parts[key] += round(detail['amount'], 2)
        return parts

    def _pending_deltas(self, runs=None, employees=None):
        """``[item values]`` — what BAS does not hold yet.

        ``runs`` limits it to those months, ``employees`` to those men. A
        month not approved or paid right now (reopened for a correction) is
        frozen: none of its items is offered, so nothing is reversed while it
        is being corrected.
        """
        scope = self._settled_runs(runs)
        desired = self._desired_events(scope)
        booked = {k: v for k, v in self._booked().items()
                  if v[1].run_id in scope}
        registers = {run.id: set(run.line_ids.employee_id.ids) for run in scope}
        vacation = self._vacation_parts(scope)
        deltas = []
        for key in list(desired) + [k for k in booked if k not in desired]:
            event = desired.get(key)
            posted, last = booked.get(key, (0.0, None))
            if not event and last.employee_id.id not in registers.get(
                    last.run_id.id, ()):
                # BAS holds it; the month's register no longer has him (paid
                # on his vacation payslip, held, never entered here). He was
                # settled somewhere else, by an entry of its own: not ours to
                # reverse, nor to show.
                continue
            target = event['amount'] if event else 0.0
            if event and event['kind'] == 'accrual' and vacation.get(key):
                # Part of this pay type was paid on his vacation payslip: what
                # BAS holds for that part stays where it is.
                target += min(max(posted - target, 0.0), vacation[key])
            delta = round(target - posted, 2)
            if abs(delta) < 0.005:
                continue
            if event:
                vals = dict(event, amount=delta)
                if last and last.counter_code and delta < 0:
                    vals.update(counter_code=last.counter_code,
                                counter_name=last.counter_name)
            else:
                # Undone after it was posted: reverse it, with what the
                # posted item knew about it.
                vals = {f: last[f] for f in self._REVERSAL_FIELDS}
                vals.update({
                    'key': key, 'run_id': last.run_id.id,
                    'run_line_id': last.run_line_id.exists().id,
                    'deduction_line_id': last.deduction_line_id.exists().id,
                    'employee_id': last.employee_id.id,
                    'bank_account_id': last.bank_account_id.id,
                    'event_date': fields.Date.context_today(self),
                    'amount': delta,
                })
            if employees is not None and vals['employee_id'] not in employees.ids:
                continue
            deltas.append(vals)
        return deltas

    @api.model
    def _pending_count(self, runs):
        """How many of ``runs``' items are not yet in any voucher."""
        return len(self.browse()._pending_deltas(runs=runs))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _collect(self):
        self.ensure_one()
        self.item_ids.sudo().unlink()
        self.env['ksw.pay.settlement.item'].sudo().create([
            dict(vals, settlement_id=self.id)
            for vals in self._pending_deltas()])

    def action_collect(self):
        """Start again from everything not yet in BAS."""
        for rec in self:
            rec._check_accountant()
            if rec.state != 'draft':
                raise UserError(_('This voucher has already been posted.'))
            rec._collect()
        return True

    def _check_fresh(self):
        """Refuse a voucher whose lines no longer say what is pending.

        Between collecting and posting, a deduction may be released or a
        second draft posted; writing the old figures would put BAS out of
        step with the very ledger this voucher is meant to keep it in.
        """
        self.ensure_one()
        pending = {v['key']: v['amount'] for v in self._pending_deltas()}
        stale = [
            '• %s — %s %.2f' % (item.employee_id.name or '',
                                item._kind_label(), item.amount)
            for item in self.item_ids
            if abs(pending.get(item.key, 0.0) - item.amount) >= 0.005
        ]
        if stale:
            raise UserError(_(
                'These transactions changed after the voucher was '
                'collected:\n\n%(rows)s\n\nPress "Refresh" and check the '
                'voucher again.', rows='\n'.join(stale[:20])))

    def action_generate(self):
        """Build the BAS file and record every line as posted."""
        self.ensure_one()
        self._check_accountant()
        if self.state != 'draft':
            raise UserError(_('This voucher has already been posted.'))
        if 'ksw.bas.journal' not in self.env:
            raise UserError(_(
                'The journal-entry export is not available on this system '
                'yet: it is written by KSW_payroll, which has not been '
                'updated. Ask IT to deploy it.'))
        if not self.item_ids:
            raise UserError(_(
                'There is nothing to post — every payment and deduction is '
                'already in BAS.'))
        if not (self.journal_number or '').strip():
            raise UserError(_('Enter the journal number before generating.'))
        self._check_fresh()
        self._generate_file()
        return self.action_download()

    def _generate_file(self):
        """Write the BAS file, attach it, and record the lines as posted."""
        self.ensure_one()
        number = (self.journal_number or '').strip()
        data = self.env['ksw.bas.journal'].build_workbook(
            self._bas_rows(), self.date,
            sheet_name='%s %s' % (_('Settlement'),
                                  self.date.strftime('%d-%m-%Y')),
            number=int(number) if number.isdigit() else number,
        )
        # Named for what it is, and for its month when it has one: the
        # accountant files these next to the month's bank files.
        prefix = {'month': 'JournalEntry', 'mark_paid': 'CommissionPayment'
                  }.get(self.origin, 'CommissionSettlement')
        month = ('_%s' % self.run_id.name) if self.run_id else ''
        attachment = self.env['ir.attachment'].sudo().create({
            'name': '%s%s_%s.xlsx' % (prefix, month,
                                      self.name.replace('/', '-')),
            'raw': data,
            'res_model': self._name,
            'res_id': self.id,
            'mimetype': 'application/vnd.openxmlformats-officedocument.'
                        'spreadsheetml.sheet',
        })
        self._mark_done(attachment=attachment)
        return attachment

    def action_mark_posted(self):
        """Record the lines as already in BAS, without a file.

        For transactions the accountant had already entered by hand before
        this voucher existed — otherwise they would be offered again on
        every voucher from now on.
        """
        self.ensure_one()
        self._check_accountant()
        if self.state != 'draft':
            raise UserError(_('This voucher has already been posted.'))
        if not self.item_ids:
            raise UserError(_('There is nothing to record.'))
        self._check_fresh()
        self._mark_done(manual=True)
        return True

    def _mark_done(self, attachment=None, manual=False):
        # What each line took from the accrual, and the account on its other
        # side, kept with it: a later reversal gives back exactly this.
        Item = self.env['ksw.pay.settlement.item'].sudo()
        # Recording what BAS already holds must not be refused for want of
        # an accrual to explain it — it is a fact, not a file being written.
        for item_id, split in self._item_splits(fallback=manual).items():
            Item.browse(item_id).write(
                {'accrual_split': [list(part) for part in split]})
        for item in self.item_ids.sudo().filtered(lambda i: not i.counter_code):
            if item.kind == 'deduction' and item.employee_id:
                try:
                    code, name = self.env['ksw.bas.journal'].loan_account(
                        item.employee_id.sudo())
                except UserError:
                    if not manual:
                        raise
                    continue     # recorded as it is; a file would name him
                item.write({'counter_code': code, 'counter_name': name})
            elif item.kind == 'payment' and item.bank_account_id:
                bank = item.bank_account_id.sudo()
                item.write({'counter_code': bank.x_bas_account_code,
                            'counter_name': bank.x_bas_account_name})
        self.sudo().write({
            'state': 'done',
            'attachment_id': attachment.id if attachment else False,
            'posted_manually': manual,
            'done_by_id': self.env.uid,
            'done_date': fields.Datetime.now(),
        })
        title = (_('Recorded as already in BAS (no file)') if manual
                 else _('Settlement voucher posted'))
        body = Markup(
            '<strong>%(title)s</strong><br/>'
            '<b>%(l_by)s</b> %(user)s<br/>'
            '<b>%(l_right)s</b> %(right).2f<br/>'
            '<b>%(l_paid)s</b> %(paid).2f<br/>'
            '<b>%(l_ded)s</b> %(ded).2f<br/>'
            '<b>%(l_n)s</b> %(n)s'
        ) % {
            'title': title,
            'l_by': _('By:'), 'user': self.env.user.name,
            'l_right': _('Commission right:'), 'right': self.total_right,
            'l_paid': _('Paid by bank:'), 'paid': self.total_paid,
            'l_ded': _('Deducted:'), 'ded': self.total_deducted,
            'l_n': _('Transactions:'), 'n': self.item_count,
        }
        self.sudo().message_post(body=body, subtype_xmlid='mail.mt_note')

    @api.model
    def _check_register(self, run, lines):
        """A register figure its entries no longer add up to has no proven
        right behind it — the same guard as the month voucher."""
        totals = run._bas_component_totals()
        mismatched = [
            '• %s — %s %.2f, %s %.2f' % (
                line.employee_id.sudo().name or '', _('register'),
                line.earnings or 0.0, _('entries'),
                sum(totals.get(line.employee_id.id, {}).values()))
            for line in lines
            if abs(round(sum(totals.get(line.employee_id.id, {}).values()), 2)
                   - round(line.earnings or 0.0, 2)) >= 0.005
        ]
        if mismatched:
            raise UserError(_(
                'These employees are paid a figure the entries no longer '
                'add up to, so the month voucher cannot have proved it and '
                'the payment has no accrual to clear:\n\n%(who)s\n\nRebuild '
                'the register (or correct the entries) first.',
                who='\n'.join(mismatched[:20])))

    @api.model
    def _check_writer(self):
        if 'ksw.bas.journal' not in self.env:
            raise UserError(_(
                'Mark Paid posts the payment to BAS, and the journal writer '
                '(KSW_payroll) is not up to date on this system. Ask IT to '
                'deploy it.'))

    @api.model
    def _post_for_run(self, run, origin, journal_number='', date=None,
                      employees=None):
        """Post everything of ``run`` BAS does not hold yet as one voucher.

        Returns the posted voucher, or an empty recordset when there is
        nothing new.
        """
        pending = self.browse()._pending_deltas(runs=run, employees=employees)
        if not pending:
            return self.browse()
        voucher = self.create({
            'origin': origin,
            'run_id': run.id,
            'journal_number': (journal_number or '').strip(),
            'date': date or fields.Date.context_today(self),
        })
        self.env['ksw.pay.settlement.item'].sudo().create([
            dict(vals, settlement_id=voucher.id) for vals in pending])
        voucher._generate_file()
        return voucher

    @api.model
    def _post_payment(self, run, lines, journal_number='', date=None):
        """Mark Paid's final settlement: the transfer, plus every difference
        BAS does not hold yet for the men being paid.

        The journal exports proved the right; this proves the issue, and
        brings each man's month up to date in the same voucher — a right
        never exported, a correction since the last export, a deduction
        taken meanwhile. Posted in the same transaction as the Mark Paid:
        if any account is missing, nothing is marked paid.
        """
        self._check_writer()
        self._check_register(run, lines)
        return self._post_for_run(
            run, 'mark_paid', journal_number=journal_number, date=date,
            employees=lines.employee_id)

    def action_rebuild_file(self):
        """Write this voucher's file again, from the same lines.

        Nothing in the record changes — the amounts, the accounts each line
        posted to and the voucher number stay as they are; only the layout
        of the file is rebuilt (after a change in how lines are grouped).
        """
        self.ensure_one()
        self._check_accountant()
        if self.state != 'done' or self.posted_manually or \
                self.origin == 'baseline':
            raise UserError(_('Only a posted voucher that has a file can be '
                              'rebuilt.'))
        number = (self.journal_number or '').strip()
        data = self.env['ksw.bas.journal'].build_workbook(
            self._bas_rows(), self.date,
            sheet_name='%s %s' % (_('Settlement'),
                                  self.date.strftime('%d-%m-%Y')),
            number=int(number) if number.isdigit() else number,
        )
        self.attachment_id.sudo().write({'raw': data})
        self.sudo().message_post(
            body=Markup('<strong>%(title)s</strong><br/><b>%(l_by)s</b> %(user)s') % {
                'title': _('File rebuilt from the same lines (layout only)'),
                'l_by': _('By:'), 'user': self.env.user.name,
            }, subtype_xmlid='mail.mt_note')
        return self.action_download()

    def action_cancel_not_imported(self):
        """The file was never imported into BAS: take it out of the record.

        Only the latest file of its months can go — a later one was worked
        out on top of this one. Its items stop counting as held by BAS, so
        the next export carries them again (corrected, if the month changed).
        """
        self.ensure_one()
        self._check_accountant()
        if self.state != 'done':
            raise UserError(_('Only a posted voucher can be cancelled.'))
        Item = self.env['ksw.pay.settlement.item'].sudo()
        later = Item.search([
            ('run_id', 'in', self.item_ids.run_id.ids),
            ('settlement_id', '>', self.id),
            ('state', '!=', 'cancel'),
        ]).settlement_id
        if later:
            raise UserError(_(
                'These later files were worked out on top of this one, so it '
                'cannot be cancelled on its own: %(names)s',
                names=', '.join(later.mapped('name'))))
        self.sudo().write({'state': 'cancel'})
        self.sudo().message_post(
            body=Markup('<strong>%(title)s</strong><br/><b>%(l_by)s</b> %(user)s') % {
                'title': _('Cancelled: this file was not imported into BAS'),
                'l_by': _('By:'), 'user': self.env.user.name,
            }, subtype_xmlid='mail.mt_note')
        return True

    def action_discard(self):
        """Delete the draft and give its transactions back."""
        self.ensure_one()
        self._check_accountant()
        if self.state != 'draft':
            raise UserError(_('Only a draft voucher can be discarded.'))
        self.unlink()
        return self._open_history()

    @api.model
    def action_new_voucher(self):
        """New Settlement Voucher: what came before, then confirm."""
        self._check_accountant()
        return {
            'type': 'ir.actions.act_window',
            'name': _('New Settlement Voucher'),
            'res_model': 'ksw.pay.settlement.new.wizard',
            'view_mode': 'form',
            'target': 'new',
        }

    def _open_form(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Settlement Voucher'),
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    @api.model
    def _open_history(self, domain=None):
        action = self.env['ir.actions.act_window']._for_xml_id(
            'KSW_commissions.action_ksw_pay_settlement')
        if domain is not None:
            action['domain'] = domain
        return action

    def action_download(self):
        self.ensure_one()
        if not self.attachment_id:
            raise UserError(_('This voucher has no file.'))
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%d?download=true' % self.attachment_id.id,
            'target': 'self',
        }

    # ------------------------------------------------------------------
    # The file
    # ------------------------------------------------------------------
    def _bas_rows(self):
        """The voucher's journal lines, month by month, man by man.

        For each man: his right (Dr expense per pay type, Cr the accrual),
        then each deduction (Dr accrual / Cr his advance), then the payment
        (Dr accrual); the bank is credited once per paying account and month,
        as a transfer is one movement on the bank statement. A negative item
        is the same lines the other way round.
        """
        self.ensure_one()
        Journal = self.env['ksw.bas.journal']
        lang = Journal.voucher_lang()
        if lang and self.env.context.get('lang') != lang:
            return self.with_context(lang=lang)._bas_rows()

        items = self.item_ids.sudo()
        self._check_settlement_accounts(items)
        splits = self._item_splits()

        def line(code, name, amount, ref, cost_center=''):
            return (Journal.row(code, name, debit=amount, ref=ref,
                                cost_center=cost_center)
                    if amount > 0 else
                    Journal.row(code, name, credit=-amount, ref=ref,
                                cost_center=cost_center))

        def reversed_label(item, label):
            return Journal.ref(_('Reversal'), label) if item.amount < 0 else label

        rows = []
        for run in items.run_id.sorted('period'):
            run_items = items.filtered(lambda i, r=run: i.run_id == r)
            month = Journal.month_label(run.period)
            bank_totals = defaultdict(float)
            people = [(e, run_items.filtered(lambda i, e=e: i.employee_id == e))
                      for e in self._employees_in_order(run, run_items)]
            orphans = run_items.filtered(lambda i: not i.employee_id)
            if orphans:
                people.append((self.env['hr.employee'], orphans))
            for employee, mine in people:
                who = Journal.employee_label(employee) if employee else ''

                def ref_for(item, label):
                    if not employee:
                        # A BAS line matched to no one: its own description
                        # already names the man and the month.
                        return reversed_label(item, label)
                    return run._bas_ref(reversed_label(item, label), who, month)

                # 1. The right.
                credits = defaultdict(float)
                names = {}
                for item in mine.filtered(lambda i: i.kind == 'accrual'):
                    rows.append(line(item.expense_code, item.expense_name,
                                     item.amount, ref_for(item, item.label),
                                     item.cost_center))
                    credits[item.accrual_code] += item.amount
                    names[item.accrual_code] = item.accrual_name
                for code, amount in credits.items():
                    amount = round(amount, 2)
                    if not amount:
                        continue
                    label = _('Monthly commissions')
                    if amount < 0:
                        label = Journal.ref(_('Reversal'), label)
                    rows.append(line(code, names[code], -amount,
                                     run._bas_ref(label, who, month)))
                # 2. What was taken out of it: one line per account — BAS has
                # one advance account per man, so his deductions are added up
                # on it, and the accrual they came out of likewise.
                deductions = mine.filtered(lambda i: i.kind == 'deduction')
                if deductions:
                    names = [n for n in deductions.sorted(
                        lambda i: (i.event_date or fields.Date.today(), i.id)
                    ).mapped('label') if n]
                    label = Journal.ref(_('Deducted from commissions'),
                                        '، '.join(dict.fromkeys(names)))
                    net = round(sum(deductions.mapped('amount')), 2)
                    if net < 0:
                        label = Journal.ref(_('Reversal'), label)
                    ref = run._bas_ref(label, who, month) if employee else label
                    accrual = defaultdict(float)
                    accrual_names = {}
                    counter = defaultdict(float)
                    counter_names = {}
                    for item in deductions:
                        sign = 1 if item.amount > 0 else -1
                        for code, name, amt in splits[item.id]:
                            accrual[code] += sign * amt
                            accrual_names[code] = name
                        code, name = (item.counter_code, item.counter_name)
                        if not code:
                            code, name = run._bas_loan_account(employee)
                        counter[code] += item.amount
                        counter_names[code] = name
                    for code, amount in accrual.items():
                        if round(amount, 2):
                            rows.append(line(code, accrual_names[code],
                                             round(amount, 2), ref))
                    for code, amount in counter.items():
                        if round(amount, 2):
                            rows.append(line(code, counter_names[code],
                                             -round(amount, 2), ref))
                # 3. The transfer.
                for item in mine.filtered(lambda i: i.kind == 'payment'):
                    sign = 1 if item.amount > 0 else -1
                    bank_totals[item.bank_account_id.sudo()] += item.amount
                    ref = ref_for(item, _('Commission paid'))
                    for code, name, amt in splits[item.id]:
                        rows.append(line(code, name, sign * amt, ref))
            for bank, amount in bank_totals.items():
                amount = round(amount, 2)
                if not amount:
                    continue
                ref = run._bas_ref(
                    Journal.ref(_('Commission transfer'),
                                bank.acc_number or ''), '', month)
                rows.append(line(bank.x_bas_account_code,
                                 bank.x_bas_account_name, -amount, ref))
        Journal.check_balanced(rows)
        return rows

    @api.model
    def _employees_in_order(self, run, items):
        """The register's own order (department, then name) — the same as
        every other file of the month — then anyone no longer on it."""
        employees = items.employee_id
        ordered = run.line_ids._export_sorted().employee_id & employees
        return ordered | (employees - ordered).sorted('name')

    def _accrual_allocations(self, run, totals):
        """``{key: [(code, name, amount)]}`` — which accrual each of the
        month's settlements draws on, replaying them in order over every
        employee's accrual pools."""
        desired = self._desired_events(run)
        allocations = {}
        for line in run.line_ids:
            employee = line.employee_id
            groups = self._accrual_groups(run, totals.get(employee.id, {}))
            taken_out = sorted(
                (e for e in desired.values()
                 if e['run_line_id'] == line.id and e['kind'] == 'deduction'),
                key=lambda e: (e['event_date'], e['key']))
            sequence = [(e['key'], e['amount']) for e in taken_out]
            payment = desired.get('payment:%d' % line.id)
            if payment:
                sequence.append((payment['key'], payment['amount']))
            for key, amount in sequence:
                taken, left = [], round(amount, 2)
                for group in groups:
                    if left <= 0.0:
                        break
                    take = round(min(group[2], left), 2)
                    if take <= 0.0:
                        continue
                    taken.append((group[0], group[1], take))
                    group[2] = round(group[2] - take, 2)
                    left = round(left - take, 2)
                if key:
                    allocations[key] = taken
        return allocations

    @api.model
    def _accrual_groups(self, run, by_component):
        """``[[code, name, amount]]`` — one employee's month per accrual
        account, in the voucher's order."""
        groups, index = [], {}
        for detail in run._bas_detail_lines(by_component):
            component = detail['component']
            code = component.x_bas_accrual_code or ''
            if code not in index:
                index[code] = len(groups)
                groups.append([code, component.x_bas_accrual_name or '', 0.0])
            groups[index[code]][2] = round(
                groups[index[code]][2] + detail['amount'], 2)
        return groups

    def _item_splits(self, fallback=False):
        """``{item id: [(code, name, amount)]}`` — the accrual accounts each
        line draws on, adding up to its amount.

        A settlement takes from the accrual pools its month left, in the
        order they were consumed. A reversal gives back exactly what the
        posted lines took — read from them, because the month it came from
        may have been reopened and no longer say.
        """
        self.ensure_one()
        items = self.item_ids.sudo()
        splits = {}
        for run in items.run_id:
            run_items = items.filtered(lambda i, r=run: i.run_id == r)
            allocations = None
            for item in run_items:
                if item.accrual_split:
                    # Stamped already (a voucher loaded from BAS).
                    source = [tuple(p) for p in item.accrual_split]
                elif item.kind == 'accrual':
                    source = [(item.accrual_code, item.accrual_name,
                               abs(item.amount))]
                elif item.amount > 0:
                    if allocations is None:
                        allocations = self._accrual_allocations(
                            run, run._bas_component_totals())
                    source = allocations.get(item.key) or []
                else:
                    posted = defaultdict(float)
                    names = {}
                    for done in self.env['ksw.pay.settlement.item'].sudo(
                            ).search([('key', '=', item.key),
                                      ('state', '=', 'done')]):
                        for code, name, amount in done.accrual_split or []:
                            sign = 1 if done.amount > 0 else -1
                            posted[code] += sign * amount
                            names[code] = name
                    source = [(code, names[code], round(amount, 2))
                              for code, amount in posted.items()
                              if round(amount, 2) > 0]
                if not source and fallback:
                    source = [self._month_accrual(run) + (abs(item.amount),)]
                splits[item.id] = self._take(source, abs(item.amount), item)
        return splits

    @api.model
    def _month_accrual(self, run):
        """``(code, name)`` — the accrual account the month's pay types
        credit most."""
        weight = defaultdict(float)
        names = {}
        for by_component in run._bas_component_totals().values():
            for component, amount in by_component.items():
                if component.x_bas_accrual_code:
                    weight[component.x_bas_accrual_code] += amount
                    names[component.x_bas_accrual_code] = \
                        component.x_bas_accrual_name or ''
        if not weight:
            component = self.env['ksw.pay.component'].sudo().search(
                [('x_bas_accrual_code', '!=', False)], limit=1)
            return component.x_bas_accrual_code, component.x_bas_accrual_name or ''
        code = max(weight, key=weight.get)
        return code, names[code]

    @api.model
    def _take(self, source, wanted, item):
        out, left = [], round(wanted, 2)
        for code, name, amount in source:
            take = round(min(amount, left), 2)
            if take > 0.0:
                out.append((code, name, take))
                left = round(left - take, 2)
        if left > 0.0:
            if not out:
                raise UserError(_(
                    '%(name)s: %(amount).2f is settled out of his %(month)s '
                    'commission, but there is no accrual left in it to take '
                    'it from.',
                    name=item.employee_id.name or '', amount=item.amount,
                    month=item.run_id.display_name or ''))
            code, name, amount = out[-1]
            out[-1] = (code, name, round(amount + left, 2))
        return out

    def _check_settlement_accounts(self, items):
        """Every account the voucher needs, named all at once."""
        problems = []
        payments = items.filtered(lambda i: i.kind == 'payment')
        no_bank = payments.filtered(lambda i: not i.bank_account_id)
        if no_bank:
            problems.append(_(
                'Paid with no paying bank account: %(names)s',
                names=', '.join(no_bank.employee_id.mapped('name'))))
        unmapped = payments.bank_account_id.sudo().filtered(
            lambda b: not b.x_bas_account_code)
        if unmapped:
            problems.append(_(
                'Paying bank accounts with no BAS account (set it on the '
                'bank account, "BAS Account Code"): %(banks)s',
                banks=', '.join(unmapped.mapped(
                    lambda b: b.acc_number or str(b.id)))))
        unaccounted = items.filtered(
            lambda i: i.kind == 'accrual'
            and not (i.expense_code and i.accrual_code))
        if unaccounted:
            problems.append(_(
                'Pay types with no BAS expense or accrual account (set them '
                'on the pay component, BAS Journal tab): %(names)s',
                names=', '.join(sorted(set(unaccounted.mapped('label'))))))
        if problems:
            raise UserError(_(
                'The settlement voucher cannot be written yet:\n\n%(rows)s',
                rows='\n'.join('• %s' % p for p in problems)))
        deducted = items.filtered(
            lambda i: i.kind == 'deduction' and not i.counter_code)
        if deducted:
            self.env['ksw.bas.journal'].check_loan_accounts(
                deducted.employee_id.sudo())


class KswPaySettlementItem(models.Model):
    _name = 'ksw.pay.settlement.item'
    _description = 'Commission Settlement Voucher Line'
    _order = 'settlement_id, run_id, employee_id, id'

    settlement_id = fields.Many2one(
        'ksw.pay.settlement', required=True, ondelete='cascade', index=True)
    state = fields.Selection(related='settlement_id.state', store=True)
    key = fields.Char(required=True, index=True, readonly=True)
    kind = fields.Selection(
        [('accrual', 'Commission Right'), ('deduction', 'Deduction'),
         ('payment', 'Paid by Bank')],
        string='Transaction', required=True, readonly=True)
    label = fields.Char(string='Description', readonly=True)
    expense_code = fields.Char(readonly=True)
    expense_name = fields.Char(readonly=True)
    accrual_code = fields.Char(readonly=True)
    accrual_name = fields.Char(readonly=True)
    cost_center = fields.Char(readonly=True)
    counter_code = fields.Char(
        string='Counter Account', readonly=True,
        help='The advance (deduction) or bank (payment) account this line '
             'posted to — stamped when posted, so a reversal uses the same.')
    counter_name = fields.Char(readonly=True)
    run_id = fields.Many2one(
        'ksw.pay.run', string='Commission Month', readonly=True,
        ondelete='restrict')
    run_line_id = fields.Many2one(
        'ksw.pay.run.line', readonly=True, ondelete='set null')
    deduction_line_id = fields.Many2one(
        'ksw.deduction.line', string='Installment', readonly=True,
        ondelete='set null')
    deduction_id = fields.Many2one(
        related='deduction_line_id.deduction_id', string='Deduction')
    employee_id = fields.Many2one('hr.employee', readonly=True)
    bank_account_id = fields.Many2one(
        'res.partner.bank', string='Paying Account', readonly=True)
    event_date = fields.Date(string='Date', readonly=True)
    currency_id = fields.Many2one(related='settlement_id.currency_id')
    amount = fields.Monetary(
        readonly=True,
        help='Negative: a reversal of something posted earlier and since '
             'undone.')
    accrual_split = fields.Json(
        readonly=True, copy=False,
        help='[[code, name, amount]] — the accrual accounts this line was '
             'posted against. Stamped when the voucher is posted.')

    def _kind_label(self):
        self.ensure_one()
        return dict(self._fields['kind']._description_selection(self.env)
                    ).get(self.kind, self.kind)

    def unlink(self):
        if not self.env.su and self.filtered(lambda i: i.state == 'done'):
            raise UserError(_(
                'A line of a posted settlement voucher cannot be removed.'))
        return super().unlink()


class KswPayRun(models.Model):
    _inherit = 'ksw.pay.run'

    x_bas_unsettled_count = fields.Integer(
        string='Not in BAS', compute='_compute_bas_unsettled_count',
        help='Changes to this month BAS does not hold yet: its right, '
             'deductions taken from it, its transfer.')

    x_settlement_count = fields.Integer(
        string='Settlement Vouchers', compute='_compute_settlement_count')
    x_bas_can_load = fields.Boolean(compute='_compute_settlement_count')

    @api.depends_context('uid')
    def _compute_bas_unsettled_count(self):
        Settlement = self.env['ksw.pay.settlement']
        is_accountant = self.env.user.has_group(ACCOUNTANT)
        for rec in self:
            rec.x_bas_unsettled_count = (
                Settlement.sudo()._pending_count(rec)
                if is_accountant and rec.id and rec.state in SETTLED_RUN_STATES
                else 0)

    def _mark_lines_paid(self, lines, journal_number='', voucher_date=None):
        """Mark Paid, and post the payment to BAS in the same step.

        Returns the posted voucher (empty when nothing had a payable
        amount).
        """
        to_pay = (lines & self.line_ids).filtered(lambda l: not l.x_paid)
        super()._mark_lines_paid(lines)
        if not to_pay:
            return self.env['ksw.pay.settlement']
        try:
            return self.env['ksw.pay.settlement']._post_payment(
                self, to_pay, journal_number=journal_number,
                date=voucher_date)
        except UserError as exc:
            raise UserError(_(
                'Payment NOT recorded: Mark Paid posts the transfer to BAS '
                'and could not.\n\n%(reason)s',
                reason=exc.args[0] if exc.args else str(exc))) from exc

    def _bas_export_month(self, journal_number='', date=None):
        """Export ▸ Journal entries: post what BAS does not hold yet.

        The first export of a month posts the whole month; every later one
        only what changed since — the earlier files stay in the month's
        history. Returns the posted voucher.
        """
        self.ensure_one()
        Settlement = self.env['ksw.pay.settlement']
        Settlement._check_accountant()
        Settlement._check_writer()
        if self.state not in SETTLED_RUN_STATES:
            raise UserError(_('Only an approved or paid month can be exported.'))
        Settlement._check_register(self, self.line_ids)
        voucher = Settlement._post_for_run(
            self, 'month', journal_number=journal_number, date=date)
        if not voucher:
            last = self._bas_vouchers()[:1]
            raise UserError(_(
                'Nothing changed since the last BAS file of this month '
                '(%(name)s, %(date)s): BAS already holds it as it stands.',
                name=last.name or '-', date=last.date or '-'))
        return voucher

    def _bas_vouchers(self):
        """Every voucher that carried this month, newest first."""
        self.ensure_one()
        return self.env['ksw.pay.settlement.item'].sudo().search(
            [('run_id', '=', self.id)]).settlement_id.sorted(
                lambda v: v.id, reverse=True)

    def action_open_bas_baseline_wizard(self):
        """Load a voucher already in BAS as this month's starting point."""
        self.ensure_one()
        self.env['ksw.pay.settlement']._check_accountant()
        # Opened unsaved, the month filled in: the voucher number is required
        # and only the accountant can type it, so creating the record here
        # failed before the dialog ever showed.
        return {
            'type': 'ir.actions.act_window',
            'name': _('Load BAS Voucher'),
            'res_model': 'ksw.pay.bas.baseline.wizard',
            'view_mode': 'form', 'target': 'new',
            'context': {'default_run_id': self.id},
        }

    def action_open_settlement(self):
        """New Settlement Voucher — which first shows the drafts holding
        transactions and the last voucher posted."""
        self.ensure_one()
        return self.env['ksw.pay.settlement'].action_new_voucher()

    @api.depends_context('uid')
    def _compute_settlement_count(self):
        Item = self.env['ksw.pay.settlement.item'].sudo()
        for rec in self:
            vouchers = Item.search([('run_id', '=', rec.id)]).settlement_id \
                if rec.id else Item.settlement_id
            rec.x_settlement_count = len(vouchers)
            # A voucher loaded from BAS is the starting point: only while the
            # month has no file that counts (a cancelled one does not).
            rec.x_bas_can_load = bool(
                rec.id and rec.state in SETTLED_RUN_STATES
                and not vouchers.filtered(lambda v: v.state != 'cancel'))

    def action_open_settlement_history(self):
        """Every voucher, draft or posted, carrying this month."""
        self.ensure_one()
        vouchers = self.env['ksw.pay.settlement.item'].sudo().search(
            [('run_id', '=', self.id)]).settlement_id
        return self.env['ksw.pay.settlement']._open_history(
            domain=[('id', 'in', vouchers.ids)])
