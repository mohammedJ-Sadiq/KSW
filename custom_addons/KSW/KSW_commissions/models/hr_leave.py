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
        """The entries the settlement of this request would pay today."""
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

    @api.depends('employee_id', 'request_date_from', 'holiday_status_id',
                 'x_commission_line_ids.amount')
    def _compute_commission_entries(self):
        for leave in self:
            applies = self._settles_commission_entries(leave)
            settled = leave._commission_entries_settled() if applies else False
            entries = settled or (
                leave._commission_entries_to_settle() if applies
                else self.env['ksw.pay.entry'])
            total = sum(entries.mapped('amount'))
            leave.x_commission_entries_applies = applies
            leave.x_commission_entries_settled = bool(settled)
            leave.x_commission_entries_count = len(entries)
            leave.x_commission_entries_total = total
            leave.x_commission_entries_html = (
                self._render_commission_entries(entries) if entries else False)
            leave.x_commission_manual_duplicate = bool(
                entries and leave.x_commission_line_ids)

    def _render_commission_entries(self, entries):
        rows = Markup()
        for entry in entries:
            what = entry.component_id.name or ''
            if entry.option_id:
                what = '%s — %s' % (what, entry.option_id.name)
            rows += Markup(
                '<tr><td>%(month)s</td><td>%(what)s</td><td>%(date)s</td>'
                '<td>%(batch)s <span class="text-muted">(%(state)s)</span></td>'
                '<td class="text-end">%(amount)s</td></tr>'
            ) % {
                'month': entry.period.strftime('%B %Y') if entry.period else '',
                'what': what,
                'date': format_date(self.env, entry.date) if entry.date else '',
                'batch': entry.batch_id.name or '',
                'state': dict(entry.batch_id._fields['state'].selection).get(
                    entry.batch_id.state, ''),
                'amount': '{:,.2f}'.format(entry.amount),
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
            'rows': rows,
            'total': '{:,.2f}'.format(sum(entries.mapped('amount'))),
        }

    # ------------------------------------------------------------------
    # The payslip
    # ------------------------------------------------------------------
    def _build_vacation_input_lines(self, leave, employee, payslip):
        vals_list = super()._build_vacation_input_lines(
            leave, employee, payslip)
        if not self._settles_commission_entries(leave):
            return vals_list
        version_id = payslip.version_id.id
        seq = 30
        for entry in leave._commission_entries_to_settle():
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
