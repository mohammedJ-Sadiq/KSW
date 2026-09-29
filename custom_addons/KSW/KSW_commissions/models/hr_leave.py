"""KSW Commissions — commission entries ride out on the vacation payslip.

The deductions twin (KSW_deduction's full deduction picture): a vacation
settles the employee's whole position at the request, and what he is still
owed in commissions is already recorded — as ``ksw.pay.entry`` rows in the
monthly batches. So the settlement reads them, rather than waiting for the
accountant to re-type them as ``x_commission_line_ids``:

  * the leave's Accounting page lists the entries the settlement pays;
  * the vacation payslip carries one ``KSW_COM_<entry_id>`` input per entry
    (summed by the ``KSW_COMMISSIONS`` salary rule);
  * confirming that payslip stamps each entry ``x_vacation_payslip_id``, so
    the monthly run leaves it out of its register (``ksw.pay.run``), and
    cancelling it releases them again (``hr.payslip.write`` in this module).

Which entries: the ones ``ksw_vacation_hold`` already says the settlement
paid — every month up to and including the departure month whose own pay
run has not been approved or paid yet. The months inside the vacation and
the part of the return month after he came back are not the settlement's
business, exactly as before.

Annual and unpaid leave only. An EOS payslip goes through the same input
builder, but the vacation hold does not hold an EOS month, so pulling the
entries there would pay them twice (once here, once in the run).
"""
from collections import defaultdict

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare
from odoo.tools.misc import format_date

from .ksw_commission_lock import LOCKING_STATES
from .ksw_vacation_hold import hold_blocks, vacation_holds


class HrLeave(models.Model):
    _inherit = 'hr.leave'

    # No model-level groups=: read through sudo(), and referenced in
    # invisible= on elements every approver sees (gotcha #31). An Html
    # render rather than a relational field on purpose: an approver has no
    # read on ksw.pay.entry, and a m2m to it would kill the form's web_read.
    x_commission_entries_applies = fields.Boolean(
        compute='_compute_commission_entries', compute_sudo=True)
    x_commission_entries_settled = fields.Boolean(
        compute='_compute_commission_entries', compute_sudo=True,
        help='The entries shown were paid on the confirmed vacation payslip '
             '(rather than being the ones it is about to pay).')
    x_commission_entries_count = fields.Integer(
        compute='_compute_commission_entries', compute_sudo=True)
    x_commission_entries_total = fields.Float(
        string='Commissions from Pay Entries', digits=(16, 2),
        compute='_compute_commission_entries', compute_sudo=True,
        help='Commission and allowance entries recorded for this employee '
             'in the Commissions app that the vacation payslip settles.')
    x_commission_entries_html = fields.Html(
        string='Commission Entries', sanitize=False,
        compute='_compute_commission_entries', compute_sudo=True)
    x_commission_pending_count = fields.Integer(
        compute='_compute_commission_entries', compute_sudo=True)
    x_commission_pending_total = fields.Float(
        string='Awaiting GM Approval', digits=(16, 2),
        compute='_compute_commission_entries', compute_sudo=True)
    x_commission_pending_html = fields.Html(
        string='Commission Entries Awaiting Approval', sanitize=False,
        compute='_compute_commission_entries', compute_sudo=True)
    x_commission_live_changed = fields.Boolean(
        compute='_compute_commission_entries', compute_sudo=True,
        help='The Commissions app no longer matches what was latched on this '
             'request.')
    x_commission_live_total = fields.Float(
        string='Commissions App Now', digits=(16, 2),
        compute='_compute_commission_entries', compute_sudo=True)
    x_can_refresh_commissions = fields.Boolean(
        compute='_compute_commission_entries', compute_sudo=True)
    # The latch itself. Stored, set only by _latch_commission_entries.
    x_commission_snapshot_ids = fields.One2many(
        'ksw.leave.commission.entry', 'leave_id', copy=False, readonly=True)
    x_commission_latched_date = fields.Datetime(
        string='Commissions Captured On', copy=False, readonly=True)
    x_commission_latched_by = fields.Many2one(
        'res.users', string='Commissions Captured By', copy=False,
        readonly=True)
    x_commission_manual_duplicate = fields.Boolean(
        compute='_compute_commission_entries', compute_sudo=True,
        help='Commission lines were typed by hand on this request while the '
             'payslip also pays the recorded entries.')

    # ------------------------------------------------------------------
    # Which entries
    # ------------------------------------------------------------------
    @staticmethod
    def _settles_commission_entries(leave):
        """Does this request's payslip pay the recorded commission entries?"""
        leave_type = leave.holiday_status_id
        # EOS: the hold does not hold its months, so the run would pay them
        # too. Extension (KSW_leave_extension): it has no payslip at all.
        if (not leave_type or getattr(leave, 'x_is_eos_leave', False)
                or getattr(leave, 'x_is_leave_extension', False)):
            return False
        return bool(leave_type.is_annual_leave or leave_type.is_unpaid_leave)

    def _commission_entries_to_settle(self):
        """The entries the settlement of this request would pay today.

        Only what the General Manager has approved: a batch reaches
        'approved' when its department's submission is approved. A row still
        being typed, or submitted but not yet signed off, is not a figure
        anyone has agreed to pay — see _commission_entries_awaiting_approval.
        """
        return self._commission_entries_outstanding().filtered(
            lambda e: e.batch_id.state == 'approved')

    def _commission_entries_awaiting_approval(self):
        """Outstanding entries the settlement does NOT pay: not approved yet.

        Listed on the leave so the accountant knows they exist; once the GM
        approves them, the accountant's Refresh at Step 4 takes them in (or,
        after the leave is closed, a Vacation Release lets the run pay them).
        """
        return self._commission_entries_outstanding().filtered(
            lambda e: e.batch_id.state != 'approved')

    def _commission_entries_outstanding(self):
        """Every entry still owed for the months this settlement covers."""
        self.ensure_one()
        Entry = self.env['ksw.pay.entry'].sudo()
        leave = self._origin or self
        if (not leave.employee_id or not leave.request_date_from
                or not self._settles_commission_entries(leave)):
            return Entry
        entries = Entry.search([
            ('employee_id', '=', leave.employee_id.id),
            ('period', '<=', leave.request_date_from.replace(day=1)),
            ('x_vacation_payslip_id', '=', False),
            ('amount', '!=', 0.0),
        ], order='period, component_id, date, id')
        if not entries:
            return entries
        # A month whose own run was approved or paid went out through the
        # register; whatever of it is still unpaid is a Vacation Release
        # question, not this settlement's.
        periods = set(entries.mapped('period'))
        locked = set(self.env['ksw.pay.run'].sudo().search([
            ('period', 'in', list(periods)),
            ('state', 'in', LOCKING_STATES),
        ]).mapped('period'))
        entries = entries.filtered(lambda e: e.period not in locked)
        # Another vacation may already speak for one of these months (back
        # to back requests): what that one holds was not earned, and is
        # not this settlement's to pay.
        blocked = Entry
        by_period = defaultdict(lambda: Entry)
        for entry in entries:
            by_period[entry.period] |= entry
        for period, rows in by_period.items():
            hold = vacation_holds(
                self.env, leave.employee_id, period).get(leave.employee_id.id)
            if not hold or hold.leave == leave:
                continue
            blocked |= rows.filtered(lambda e, h=hold: hold_blocks(h, e.date))
        return entries - blocked

    def _commission_entries_settled(self):
        """The entries a confirmed payslip of this request already paid."""
        self.ensure_one()
        leave = self._origin or self
        if not leave.id:
            return self.env['ksw.pay.entry'].sudo()
        return self.env['ksw.pay.entry'].sudo().search([
            ('x_vacation_payslip_id.x_leave_id', '=', leave.id),
        ], order='period, component_id, date, id')

    # ------------------------------------------------------------------
    # The latch: what Accounting reviewed is what the request keeps
    # ------------------------------------------------------------------
    # The request is a record of a decision. Read live, the table would
    # show whatever the Commissions app says today — a month the GM
    # approved after the vacation was calculated would simply appear on a
    # request that never paid it (dev leave 47147, Sep 2026). So the
    # entries are copied onto the request the first time it reaches the
    # accountant (Step 4), and only the accountant's Refresh button, at
    # that step, copies them again. A return to Step 4 does not refresh by
    # itself: the accountant decides.
    _COMMISSION_PAST_ACC = (
        'pending_gm_final', 'pending_employee_signature', 'approved')

    def _latch_commission_entries(self):
        """Copy what the settlement would pay now onto the request."""
        Snapshot = self.env['ksw.leave.commission.entry'].sudo()
        for leave in self:
            leave.sudo().x_commission_snapshot_ids.unlink()
            vals = []
            if self._settles_commission_entries(leave):
                for included, entries in (
                        (True, leave._commission_entries_to_settle()),
                        (False, leave._commission_entries_awaiting_approval())):
                    vals += [
                        Snapshot._vals_from_entry(leave, entry, included)
                        for entry in entries]
            Snapshot.create(vals)
            leave.sudo().write({
                'x_commission_latched_date': fields.Datetime.now(),
                'x_commission_latched_by': self.env.uid,
            })

    def write(self, vals):
        res = super().write(vals)
        if vals.get('x_annual_approval_state') == 'pending_acc':
            first_time = self.filtered(
                lambda l: not l.x_commission_latched_date
                and self._settles_commission_entries(l))
            if first_time:
                first_time._latch_commission_entries()
        return res

    def action_refresh_commission_entries(self):
        """Step 4 only, this request's accountant only: fetch again."""
        self.ensure_one()
        if self.x_annual_approval_state != 'pending_acc':
            raise UserError(_(
                "Commissions can only be refreshed while the request is with "
                "Accounting (Step 4). Return it to Accounting first."))
        self._check_department_accountant(self)
        before = sum(self.sudo().x_commission_snapshot_ids.filtered(
            'included').mapped('amount'))
        self._latch_commission_entries()
        after = sum(self.sudo().x_commission_snapshot_ids.filtered(
            'included').mapped('amount'))
        self.sudo().message_post(
            body=Markup(
                '<strong>%(title)s</strong><br/>'
                '<b>%(l_before)s</b> %(before).2f<br/>'
                '<b>%(l_after)s</b> %(after).2f<br/>'
                '<b>%(l_by)s</b> %(user)s'
            ) % {
                'title': _('Commissions refreshed from the Commissions app'),
                'l_before': _('Before:'), 'before': before,
                'l_after': _('After:'), 'after': after,
                'l_by': _('By:'), 'user': self.env.user.name,
            },
            subtype_xmlid='mail.mt_note',
        )
        return True

    def _commission_rows_for_display(self):
        """``(included, awaiting)`` rows for the table — latched if the
        request has been latched, live only before it reached Accounting."""
        self.ensure_one()
        leave = self._origin or self
        if leave.x_commission_latched_date:
            lines = leave.sudo().x_commission_snapshot_ids
            return (
                [l._as_row() for l in lines if l.included],
                [l._as_row() for l in lines if not l.included])
        Snapshot = self.env['ksw.leave.commission.entry']
        settled = leave._commission_entries_settled()
        if settled:
            # Unlatched but paid: what the payslip paid is the record.
            return [Snapshot._row_from_entry(e) for e in settled], []
        if leave.x_annual_approval_state in self._COMMISSION_PAST_ACC:
            # Past Step 4 without a latch (a request already there when this
            # was installed): nothing was agreed, so nothing is shown.
            return [], []
        return (
            [Snapshot._row_from_entry(e)
             for e in leave._commission_entries_to_settle()],
            [Snapshot._row_from_entry(e)
             for e in leave._commission_entries_awaiting_approval()])

    # uid: the batch links and the Refresh button depend on who is looking.
    @api.depends_context('uid')
    @api.depends('employee_id', 'request_date_from', 'holiday_status_id',
                 'x_annual_approval_state', 'x_commission_latched_date',
                 'x_commission_snapshot_ids.amount',
                 'x_commission_line_ids.amount')
    def _compute_commission_entries(self):
        for leave in self:
            applies = self._settles_commission_entries(leave)
            included, pending = (
                leave._commission_rows_for_display() if applies else ([], []))
            settled = (leave._commission_entries_settled() if applies
                       else self.env['ksw.pay.entry'])
            leave.x_commission_entries_applies = applies
            leave.x_commission_entries_settled = bool(settled)
            leave.x_commission_entries_count = len(included)
            leave.x_commission_entries_total = sum(
                r['amount'] for r in included)
            leave.x_commission_entries_html = (
                self._render_commission_rows(included) if included else False)
            leave.x_commission_manual_duplicate = bool(
                included and leave.x_commission_line_ids)
            leave.x_commission_pending_count = len(pending)
            leave.x_commission_pending_total = sum(
                r['amount'] for r in pending)
            leave.x_commission_pending_html = (
                self._render_commission_rows(pending) if pending else False)

            # Has the Commissions app moved since the latch? Compared on what
            # is still to be paid: the live set excludes what this request's
            # payslip already paid, so drop those from the latched side too.
            # A request past Step 4 that was never latched agreed to nothing,
            # so anything payable now is a change too.
            changed, live_total = False, 0.0
            if applies and (
                    leave.x_commission_latched_date
                    or leave.x_annual_approval_state in self._COMMISSION_PAST_ACC):
                live = leave._commission_entries_to_settle()
                live_total = sum(live.mapped('amount'))
                latched = {
                    l.entry_id.id: l.amount
                    for l in leave.sudo().x_commission_snapshot_ids
                    if l.included and l.entry_id not in settled}
                changed = latched != {e.id: e.amount for e in live}
            leave.x_commission_live_changed = changed
            leave.x_commission_live_total = live_total
            leave.x_can_refresh_commissions = bool(
                applies and leave.x_annual_approval_state == 'pending_acc'
                and (self.env.user.has_group('base.group_system')
                     or self.env.user in leave._department_accountant_users(
                         leave)))

    def _render_commission_rows(self, rows):
        # The batch number links to the batch for review — but only for a
        # viewer who can open it. The compute runs as sudo(), so the question
        # is asked as the real user; an approver outside the Commissions
        # roles gets the number as text rather than a link to an access
        # error.
        batches = self.env['ksw.pay.batch'].browse(
            [r['batch'].id for r in rows if r['batch']]).exists()
        readable = batches.with_env(
            self.env(su=False))._filtered_access('read')
        body = Markup()
        for row in rows:
            batch = row['batch']
            if batch and batch in readable:
                batch_ref = Markup(
                    '<a href="/odoo/ksw.pay.batch/%(id)s" target="_blank" '
                    'title="%(title)s">%(name)s</a>'
                ) % {'id': batch.id, 'name': row['batch_name'] or '',
                     'title': _('Open the batch')}
            else:
                batch_ref = row['batch_name'] or ''
            body += Markup(
                '<tr><td>%(month)s</td><td>%(what)s</td><td>%(date)s</td>'
                '<td>%(batch)s <span class="text-muted">(%(state)s)</span></td>'
                '<td class="text-end">%(amount)s</td></tr>'
            ) % {
                'month': row['period'].strftime('%B %Y') if row['period'] else '',
                'what': row['what'],
                'date': format_date(self.env, row['date']) if row['date'] else '',
                'batch': batch_ref,
                'state': row['state'],
                'amount': '{:,.2f}'.format(row['amount']),
            }
        return Markup(
            '<table class="table table-sm o_ksw_commission_entries">'
            '<thead><tr><th>%(h_month)s</th><th>%(h_what)s</th>'
            '<th>%(h_date)s</th><th>%(h_batch)s</th>'
            '<th class="text-end">%(h_amount)s</th></tr></thead>'
            '<tbody>%(rows)s</tbody>'
            '<tfoot><tr><th colspan="4">%(h_total)s</th>'
            '<th class="text-end">%(total)s</th></tr></tfoot></table>'
        ) % {
            'h_month': _('Month'), 'h_what': _('Pay Type'),
            'h_date': _('Date'), 'h_batch': _('Batch'),
            'h_amount': _('Amount'), 'h_total': _('Total'),
            'rows': body,
            'total': '{:,.2f}'.format(sum(r['amount'] for r in rows)),
        }

    # ------------------------------------------------------------------
    # The payslip
    # ------------------------------------------------------------------
    def _commission_entries_for_payslip(self, leave):
        """What the payslip pays: the latch, never the live app.

        A latched line is paid only if its entry still says exactly what was
        latched and is still payable — approved, unpaid, in an open month.
        Anything that moved since is left out rather than swapped for the
        live figure; the request then shows it as changed, and the
        accountant's Refresh is the way to take the new figure in.
        """
        Entry = self.env['ksw.pay.entry'].sudo()
        if leave.x_commission_latched_date:
            payable = leave._commission_entries_to_settle()
            entries = Entry
            for line in leave.sudo().x_commission_snapshot_ids.filtered(
                    'included'):
                entry = line.entry_id
                if (entry and entry in payable
                        and float_compare(entry.amount, line.amount,
                                          precision_digits=2) == 0):
                    entries |= entry
            return entries
        if leave.x_annual_approval_state in self._COMMISSION_PAST_ACC:
            return Entry
        # Before Step 4 (a provisional calculation): nothing latched yet.
        return leave._commission_entries_to_settle()

    def _build_vacation_input_lines(self, leave, employee, payslip):
        vals_list = super()._build_vacation_input_lines(
            leave, employee, payslip)
        if not self._settles_commission_entries(leave):
            return vals_list
        version_id = payslip.version_id.id
        seq = 30
        for entry in self._commission_entries_for_payslip(leave):
            label = '%s — %s' % (
                entry.option_id.name or entry.component_id.name or '',
                entry.period.strftime('%m/%Y'))
            if entry.date:
                label += ' (%s)' % format_date(self.env, entry.date)
            vals_list.append({
                'payslip_id': payslip.id,
                'version_id': version_id,
                'name': label,
                'code': 'KSW_COM_%d' % entry.id,
                'amount': entry.amount,
                'sequence': seq,
            })
            seq += 1
        return vals_list
