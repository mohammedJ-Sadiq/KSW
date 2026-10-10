"""Extensions of ksw.deduction for the KSW_commissions awaiting-commission flow.
* Override ``_generate_installment_lines`` to stamp ``x_original_amount``
  on each newly-created line (audit trail).
* Add ``_get_pending_commission_lines_for_period`` helper used by the
  commission sheet to compute the auto-pulled "Loans" deductible.
"""
import math

from markupsafe import Markup

from odoo import _, api, fields, models


class KswDeduction(models.Model):
    _inherit = 'ksw.deduction'

    # ------------------------------------------------------------------
    # Stamp x_original_amount on newly-generated installment lines.
    # ------------------------------------------------------------------
    def _generate_installment_lines(self):
        before = set(self.line_ids.ids)
        super()._generate_installment_lines()
        new_lines = self.line_ids.filtered(lambda l: l.id not in before)
        for line in new_lines:
            # Bypass the line write override (which gates amount changes
            # behind group_installment_edit) — this is internal
            # scaffolding, not a user edit. We only set x_original_amount;
            # ``amount`` itself is untouched.
            line.sudo().write({'x_original_amount': line.amount})

    def write(self, vals):
        res = super().write(vals)
        if 'state' in vals:
            # Only an active deduction's installments count towards an open
            # month's loan estimate, and rows are generated before the
            # deduction turns active.
            self.line_ids._refresh_pay_run_estimates()
        return res

    # ------------------------------------------------------------------
    # Added step: take non-loan deductions out of unpaid commission
    # ------------------------------------------------------------------
    def _activate_and_generate_lines(self):
        res = super()._activate_and_generate_lines()
        self._recover_from_unpaid_commission()
        return res

    def _recover_from_unpaid_commission(self):
        """Take what a non-loan deduction owes now out of commission that is
        approved but not yet marked Paid, oldest month first.

        Only an added step. A personal loan is never touched (it follows its
        schedule), and nothing about how the rest is collected changes: what
        the commission cannot cover stays a plain pending installment, which
        the next payslip or the next approved commission month collects,
        whichever comes first (the latter through ``_finalise_month``).

        The installment keeps its own due month; the commission month it was
        taken from is ``x_recover_from_run_id``, and it is posted to BAS as
        its own record (``ksw.pay.run._bas_recovery_rows``).

        Returns the installment lines it settled.
        """
        Line = self.env['ksw.deduction.line'].sudo()
        RunLine = self.env['ksw.pay.run.line'].sudo()
        today = fields.Date.context_today(self)
        this_month = today.replace(day=1)
        quiet = {'_skip_installment_total_check': True}
        settled = Line
        for ded in self.sudo():
            if ded.state != 'active' or ded.type_id.is_loan:
                continue
            due = ded.line_ids.filtered(
                lambda l: l.state == 'pending' and not l.x_awaiting_commission
                and l.amount > 0 and l.period_date
                and l.period_date <= this_month
            ).sorted(lambda l: (l.period_date, l.sequence, l.id))
            if not due:
                continue
            sources = RunLine.search([
                ('employee_id', '=', ded.employee_id.id),
                ('run_id.state', '=', 'approved'),
            ], order='period, id')
            taken = []
            for line in due:
                for run_line in sources:
                    if line.state != 'pending' or line.amount < 1.0:
                        break
                    take = min(math.floor(line.amount + 1e-6),
                               run_line._recovery_available())
                    if take <= 0:
                        continue
                    target = line
                    if take < line.amount - 1e-6:
                        target = Line.with_context(
                            _ksw_auto_generating=True, **quiet).create({
                                'deduction_id': ded.id,
                                'sequence': line.sequence,
                                'year': line.year,
                                'month': line.month,
                                'amount': take,
                                'state': 'pending',
                                'is_manual': False,
                                'x_original_amount': take,
                            })
                        line.with_context(**quiet).write(
                            {'amount': line.amount - take})
                    target.with_context(**quiet).write({
                        'x_awaiting_commission': True,
                        'x_recover_from_run_id': run_line.run_id.id,
                        'x_recovery_date': today,
                        'x_recovery_by': self.env.uid,
                    })
                    ded._validate_installments_total()
                    run_line._apply_recovery_now(target)
                    if target.state == 'paid':
                        settled |= target
                        taken.append((run_line.run_id, target.amount))
            if taken:
                ded._post_commission_recovery(taken)
        if settled:
            settled._recompute_draft_payslips_after_recovery()
        return settled

    def _post_commission_recovery(self, taken):
        self.ensure_one()
        body = Markup('<strong>%(title)s</strong><br/>') % {
            'title': _('Taken from unpaid commission')}
        for run, amount in taken:
            body += Markup('• %(month)s: %(amt).2f<br/>') % {
                'month': run.display_name or '', 'amt': amount}
        left = sum(self.line_ids.filtered(
            lambda l: l.state == 'pending').mapped('amount'))
        if left:
            body += Markup('<i>%(msg)s</i>') % {'msg': _(
                '%(left).2f is still owed: the next payslip or the next '
                'approved commission month collects it, whichever comes '
                'first.', left=left)}
        self.message_post(body=body, subtype_xmlid='mail.mt_note')

    @api.model
    def _recover_for_approved_run(self, run):
        """The 'next commission' half of point 4: a month just approved
        collects what earlier non-loan deductions still owe."""
        employees = run.sudo().line_ids.employee_id
        if not employees:
            return
        deductions = self.sudo().search([
            ('employee_id', 'in', employees.ids),
            ('state', '=', 'active'),
            ('type_id.is_loan', '=', False),
        ], order='x_charge_date, id')
        deductions._recover_from_unpaid_commission()

    # ------------------------------------------------------------------
    # Helpers — used by ksw.commission.sheet
    # ------------------------------------------------------------------
    @staticmethod
    def _period_to_year_month(period_date):
        d = fields.Date.to_date(period_date)
        return d.year, d.month

    @api.model
    def _get_pending_commission_lines_for_period(self, employee, period_date):
        """Return ``(total, lines)`` for an employee's pending
        installments parked for commission settlement in the (year,
        month) of ``period_date``.

        ``total`` is the sum of every PENDING ``ksw.deduction.line.amount``
        with ``x_awaiting_commission=True`` that falls in the target
        month for the given employee, restricted to deductions in
        state ``'active'``.

        ``lines`` is the matching recordset, ordered FIFO across loans
        (oldest deduction first, then by sequence) — the commission
        sheet's ``done`` walker uses this order to decide which lines
        to pay through the commission first when the locked amount only
        covers part of the month's parked installments.
        """
        if not employee or not period_date:
            return 0.0, self.env['ksw.deduction.line']
        year, month = self._period_to_year_month(period_date)
        period = fields.Date.to_date(period_date).replace(day=1)
        Line = self.env['ksw.deduction.line'].sudo()
        # Two ways to be owed by this month: parked in it by due month, or
        # reserved for it by name (``x_recover_from_run_id``) — which takes
        # the row out of its own due month's run.
        lines = Line.search([
            ('employee_id', '=', employee.id),
            ('state', '=', 'pending'),
            ('x_awaiting_commission', '=', True),
            ('deduction_id.state', '=', 'active'),
            '|',
            '&', ('x_recover_from_run_id', '=', False),
            '&', ('year', '=', year), ('month', '=', month),
            ('x_recover_from_run_id.period', '=', period),
        ]).sorted(key=lambda l: (
            # Reservations last: they were decided after the month's own
            # installments, and must never push one of those to payroll.
            bool(l.x_recover_from_run_id),
            l.x_recovery_date or fields.Date.to_date('1900-01-01'),
            l.deduction_id.create_date or fields.Datetime.now(),
            l.sequence, l.id,
        ))
        total = sum(lines.mapped('amount'))
        return total, lines
