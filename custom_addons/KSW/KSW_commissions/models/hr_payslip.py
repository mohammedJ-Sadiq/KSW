"""Override ``hr.payslip`` to keep KSW deduction lines flagged
``x_awaiting_commission`` out of the payroll input list.

Lines parked on a loan with ``x_awaiting_commission=True`` are by
definition routed to the employee's monthly KSW commission sheet
instead of payroll. Without this override they would be deducted
twice (once via salary, once via commission).

The override post-filters the parent's results rather than rerunning
the search from scratch — minimal behavioural change, no duplicated
SQL.

It also settles the commission entries a vacation payslip carries
(``KSW_COM_<entry_id>``, built by ``hr.leave._build_vacation_input_lines``
in this module): confirming the payslip stamps them paid, cancelling or
resetting it releases them to their monthly run again — the twin of
KSW_deduction's ``_sync_deductions_on_done`` / ``_on_reset``.
"""
from markupsafe import Markup

from odoo import _, api, models
from odoo.exceptions import UserError


#: Input code prefix for a commission entry paid on a vacation payslip —
#: ``KSW_COM_<ksw.pay.entry id>``. Summed by the KSW_COMMISSIONS rule.
COMMISSION_INPUT_PREFIX = 'KSW_COM_'


class HrPayslip(models.Model):
    _inherit = 'hr.payslip'

    @api.model
    def get_inputs(self, versions, date_from, date_to):
        res = super().get_inputs(versions, date_from, date_to)
        if not res:
            return res
        Line = self.env['ksw.deduction.line'].sudo()
        # Each KSW deduction input has code ``KSW_DED_<line_id>``.
        # Build a set of line ids flagged awaiting-commission so we
        # can drop them from the input list. Doing this in one
        # search avoids N round-trips when the payslip covers a
        # heavy month.
        candidate_ids = []
        for inp in res:
            code = inp.get('code') or ''
            if code.startswith('KSW_DED_') and code[8:].isdigit():
                candidate_ids.append(int(code[8:]))
        if not candidate_ids:
            return res
        parked_ids = set(Line.search([
            ('id', 'in', candidate_ids),
            ('x_awaiting_commission', '=', True),
        ]).ids)
        if not parked_ids:
            return res
        return [
            inp for inp in res
            if not (
                (inp.get('code') or '').startswith('KSW_DED_')
                and (inp['code'][8:].isdigit())
                and int(inp['code'][8:]) in parked_ids
            )
        ]

    def _inject_ksw_deduction_inputs(self, payslip):
        super()._inject_ksw_deduction_inputs(payslip)
        # Drop KSW_DED_* input lines whose underlying installment is
        # parked for commission settlement.
        ksw_inputs = payslip.input_line_ids.filtered(
            lambda i: i.code and i.code.startswith('KSW_DED_')
            and i.code[8:].isdigit()
        )
        if not ksw_inputs:
            return
        line_ids = [int(i.code[8:]) for i in ksw_inputs]
        parked_ids = set(self.env['ksw.deduction.line'].sudo().search([
            ('id', 'in', line_ids),
            ('x_awaiting_commission', '=', True),
        ]).ids)
        if not parked_ids:
            return
        to_drop = ksw_inputs.filtered(
            lambda i: int(i.code[8:]) in parked_ids)
        if to_drop:
            to_drop.sudo().unlink()

    # ------------------------------------------------------------------
    # Commission entries settled on a vacation payslip
    # ------------------------------------------------------------------
    def write(self, vals):
        new_state = vals.get('state')
        prev = {s.id: s.state for s in self} if new_state else {}
        res = super().write(vals)
        if new_state:
            for slip in self:
                old = prev.get(slip.id)
                if new_state == 'done' and old != 'done':
                    slip._ksw_settle_commission_entries()
                elif new_state in ('draft', 'cancel') and old == 'done':
                    slip._ksw_release_commission_entries()
        return res

    def _ksw_commission_entry_ids(self):
        self.ensure_one()
        n = len(COMMISSION_INPUT_PREFIX)
        return [
            int(i.code[n:]) for i in self.input_line_ids
            if i.code and i.code.startswith(COMMISSION_INPUT_PREFIX)
            and i.code[n:].isdigit()
        ]

    def _ksw_settles_commissions(self):
        """Is this the payslip that pays the entries it carries?

        A revision carries the KSW_COM_ inputs forward so its deserved NET
        is right, but the entries were paid by the payslip it revises —
        stamping them again would move them to the wrong document. A
        provisional preview settles nothing.
        """
        self.ensure_one()
        return bool(
            self.x_leave_id and not self.x_is_revision
            and not self.x_is_vacation_preview)

    def _ksw_settle_commission_entries(self):
        """Mark the entries this payslip pays, so the run does not pay them.

        Raises rather than paying twice: an entry already paid elsewhere, or
        a month whose run has since been marked Paid with a line for him,
        means the payslip is stale — Recompute Vacation Payslip
        rebuilds it from what is still owed.
        """
        self.ensure_one()
        if not self._ksw_settles_commissions():
            return
        entries = self.env['ksw.pay.entry'].sudo().browse(
            self._ksw_commission_entry_ids()).exists()
        if not entries:
            return
        elsewhere = entries.filtered(
            lambda e: e.x_vacation_payslip_id
            and e.x_vacation_payslip_id != self)
        # A month counts as paid only once its run is marked Paid and has
        # a line for him; an approved month is taken out of its register
        # below (_resync_vacation_line).
        paid_months = self.env['ksw.pay.run']._months_paid_to(
            self.employee_id, set(entries.mapped('period')))
        locked = entries.filtered(lambda e: e.period in paid_months)
        # Only what the GM approved may be paid — the builder already
        # filters on it; this catches a payslip confirmed by hand later.
        unapproved = entries.filtered(lambda e: e.state != 'approved')
        if unapproved:
            raise UserError(_(
                "%(slip)s pays commission entries the General Manager has not "
                "approved:\n%(rows)s\n\nRecompute the vacation payslip from "
                "the leave request before confirming it.",
                slip=self.number or self.name,
                rows='\n'.join(
                    '\u2022 %s \u2014 %s (%s)' % (
                        e.display_name, e.period.strftime('%B %Y'),
                        e.batch_id.name)
                    for e in unapproved)))
        if elsewhere or locked:
            raise UserError(_(
                "%(slip)s pays commission entries that have been paid since "
                "it was calculated:\n%(rows)s\n\nRecompute the vacation "
                "payslip from the leave request before confirming it.",
                slip=self.number or self.name,
                rows='\n'.join(
                    '\u2022 %s \u2014 %s' % (
                        e.display_name, e.period.strftime('%B %Y'))
                    for e in (elsewhere | locked))))
        entries.with_context(ksw_vacation_settling=True).write(
            {'x_vacation_payslip_id': self.id})
        self._ksw_resync_approved_runs(entries)
        self._ksw_post_commission_note(
            entries, _('Commission entries paid on this payslip'))

    def _ksw_release_commission_entries(self):
        """The payslip no longer pays them: the monthly run may again."""
        self.ensure_one()
        entries = self.env['ksw.pay.entry'].sudo().search([
            ('x_vacation_payslip_id', '=', self.id)])
        if not entries:
            return
        entries.with_context(ksw_vacation_settling=True).write(
            {'x_vacation_payslip_id': False})
        self._ksw_resync_approved_runs(entries)
        self._ksw_post_commission_note(
            entries, _('Commission entries released — payable in their '
                       'monthly pay run again'))

    def _ksw_resync_approved_runs(self, entries):
        """Keep the month's register in step, at once, with what this
        payslip took from it or gave back: an approved (not yet paid) run
        is resynced line by line, an open one has its preview rebuilt."""
        runs = self.env['ksw.pay.run'].sudo().search([
            ('period', 'in', list(set(entries.mapped('period')))),
            ('state', '!=', 'paid'),
        ])
        runs.filtered(lambda r: r.state == 'approved')._resync_vacation_line(
            entries.employee_id)
        runs.filtered(lambda r: r.state != 'approved')._refresh_register()

    @api.model
    def _ksw_drop_committed_commissions(self, run, employees):
        """``run`` has just committed its month to ``employees`` (bank file
        exported, or marked Paid). Any vacation / EOS payslip not yet
        confirmed that still carries one of those entries loses it now, so
        it cannot be confirmed into a second payment of the same month.
        Previews included: a provisional figure should not show money the
        run is paying."""
        Entry = self.env['ksw.pay.entry'].sudo()
        entries = Entry.search([
            ('period', '=', run.period),
            ('employee_id', 'in', employees.ids),
            ('x_vacation_payslip_id', '=', False),
        ])
        if not entries:
            return
        codes = {'%s%d' % (COMMISSION_INPUT_PREFIX, e.id): e for e in entries}
        inputs = self.env['hr.payslip.input'].sudo().search([
            ('code', 'in', list(codes)),
            ('payslip_id.state', 'not in', ('done', 'cancel')),
            ('payslip_id.x_leave_id', '!=', False),
        ])
        for slip in inputs.payslip_id:
            mine = inputs.filtered(lambda i, s=slip: i.payslip_id == s)
            dropped = Entry.browse([codes[i.code].id for i in mine])
            mine.unlink()
            slip.compute_sheet()
            title = _('Commission entries removed: %(run)s pays them',
                      run=run.display_name)
            slip._ksw_post_commission_note(dropped, title)
            slip.x_leave_id.sudo().message_post(
                body=Markup('<strong>%(title)s</strong><br/>%(slip)s') % {
                    'title': title, 'slip': slip.number or slip.name},
                subtype_xmlid='mail.mt_note')

    def _ksw_post_commission_note(self, entries, title):
        body = Markup('<strong>%(title)s</strong><br/>') % {'title': title}
        for entry in entries:
            body += Markup('\u2022 %(what)s \u2014 %(month)s: %(amt).2f<br/>') % {
                'what': entry.option_id.name or entry.component_id.name or '',
                'month': entry.period.strftime('%B %Y'),
                'amt': entry.amount,
            }
        body += Markup('<b>%(label)s</b> %(total).2f') % {
            'label': _('Total:'), 'total': sum(entries.mapped('amount'))}
        self.sudo().message_post(body=body, subtype_xmlid='mail.mt_note')

