"""Extension of ksw.deduction.line for the commission-shortfall flow.

Adds three fields used by KSW_commissions:

* ``x_original_amount`` — captured on auto-generation; audit trail of
  the originally-scheduled installment amount (kept for traceability
  even though the new "park-for-commission" flow no longer relies on
  it for the live loans figure).
* ``x_paid_via_pay_run_line_id`` — set on each pending installment
  when a commission sheet's ``done`` action consumes it. Lets the
  reset path unwind the offset cleanly.
* ``x_awaiting_commission`` — accountant-set Boolean on a PENDING line
  meaning "this slice of the month's installment is to be recouped
  from the employee's commission sheet, not from payroll". Payroll
  skips lines with this flag (see ``hr_payslip._inject_ksw_deduction_inputs``
  override). The matching commission sheet's ``Loans (auto)`` field
  reads exactly the sum of these lines for the (employee, year, month).

We also override ``_generate_installment_lines`` (on the parent
``ksw.deduction``) to populate ``x_original_amount`` at creation time.
"""
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class KswDeductionLine(models.Model):
    _inherit = 'ksw.deduction.line'

    x_original_amount = fields.Monetary(
        string='Originally Scheduled Amount', readonly=True, copy=False,
        help='Audit-only: amount this installment was created with '
             '(auto-generated or manually entered). The "Awaiting '
             'Commission" flag is now the authoritative signal that '
             'a slice of this month is being routed to the commission '
             'sheet — this field is kept purely for traceability.',
    )
    x_paid_via_pay_run_line_id = fields.Many2one(
        'ksw.pay.run.line', readonly=True, copy=False,
        ondelete='restrict',
        string='Paid via Pay Run',
        help='Set when an approved monthly pay run settled this '
             'installment out of the commission payment. Lets the '
             'reopen path unwind the offset.',
    )
    x_awaiting_commission = fields.Boolean(
        string='Awaiting Commission',
        default=False,
        copy=False,
        help='Tick on a PENDING installment line to mean: do not '
             'deduct this slice from payroll — recoup it from the '
             'employee\'s monthly commission sheet instead. The '
             'matching commission sheet automatically picks it up '
             'in its "Loans (auto)" total. Cleared automatically '
             'when the line is paid via a commission sheet.',
    )
    # Non-stored helper: the commission sheet that *would* settle this
    # awaiting-commission line (matched by employee + year + month).
    # Computed only when ``x_awaiting_commission`` is True and the
    # line is still pending — once finalise stamps
    # ``x_paid_via_pay_run_line_id``, that field is the
    # authoritative link instead.
    x_pending_pay_run_id = fields.Many2one(
        'ksw.pay.run',
        string='Pending Pay Run',
        compute='_compute_pending_pay_run', store=True,
        help='The (existing) monthly pay run that will settle this '
             'parked installment when approved. Empty if no run '
             'exists yet for that month.',
    )

    # ------------------------------------------------------------------
    # Recovery from a chosen commission month
    # ------------------------------------------------------------------
    # The month an installment is *due* and the commission it is *recovered
    # from* are two different facts. Matching by (year, month) made them
    # one, so taking a September penalty out of the still-unpaid August
    # commission meant relabelling the penalty as August, and the August
    # journal (dated 31 Aug) then credited a debt that did not exist yet.
    # The commission month is a field of its own, set automatically for
    # non-loan deductions by ``ksw.deduction._recover_from_unpaid_commission``.
    x_recover_from_run_id = fields.Many2one(
        'ksw.pay.run', string='Recover from Commission',
        readonly=True, copy=False, index=True, ondelete='restrict',
        help='The approved, unpaid commission month this installment was '
             'taken from. The installment keeps its own due month.',
    )
    x_recovery_date = fields.Date(
        string='Recovery Decided On', readonly=True, copy=False,
    )
    x_recovery_by = fields.Many2one(
        'res.users', string='Recovery Decided By', readonly=True, copy=False,
    )
    def _recovery_posting_date(self):
        """The date this recovery is posted on.

        The commission is first credited in full to the accrual pool on the
        month end. A deduction that already existed then is taken off in
        that same voucher. One charged later is taken off on its own
        voucher, dated the day it was charged. So the date is the later of
        the two, and never before either the debt or the accrual exists.
        """
        self.ensure_one()
        run = self.x_recover_from_run_id.sudo()
        candidates = [d for d in (
            self.deduction_id.sudo().x_charge_date,
            run.period and run._bas_journal_date(),
        ) if d]
        return max(candidates) if candidates else fields.Date.context_today(self)

    def _recompute_draft_payslips_after_recovery(self):
        """A draft payslip computed before the recovery still carries the
        installment (``KSW_DED_<id>``); recompute it so it stops collecting
        what the commission has just collected. Revisions are frozen."""
        codes = ['KSW_DED_%d' % l.id
                 for l in self.sudo().deduction_id.line_ids]
        slips = self.env['hr.payslip.input'].sudo().search([
            ('code', 'in', codes),
            ('payslip_id.state', '=', 'draft'),
            ('payslip_id.x_is_revision', '=', False),
        ]).payslip_id
        for slip in slips:
            slip.compute_sheet()

    def _release_recovery(self, reason):
        """Hand a reservation back to payroll, saying why on the deduction."""
        released = self.filtered('x_recover_from_run_id')
        for line in released:
            run = line.x_recover_from_run_id.sudo()
            line.deduction_id.sudo().message_post(
                body=Markup(
                    '<strong>%(title)s</strong><br/>'
                    '<b>%(l_month)s</b> %(month)s<br/>'
                    '<b>%(l_amt)s</b> %(amt).2f<br/>%(reason)s'
                ) % {
                    'title': _('Commission recovery released to payroll'),
                    'l_month': _('Commission month:'),
                    'month': run.display_name or '',
                    'l_amt': _('Amount:'), 'amt': line.amount,
                    'reason': reason,
                },
                subtype_xmlid='mail.mt_note',
            )
        released.sudo().with_context(_skip_installment_total_check=True).write({
            'x_awaiting_commission': False,
            'x_recover_from_run_id': False,
            'x_recovery_date': False,
            'x_recovery_by': False,
        })

    @api.depends('x_awaiting_commission', 'state', 'employee_id',
                 'year', 'month', 'x_recover_from_run_id')
    def _compute_pending_pay_run(self):
        Run = self.env['ksw.pay.run'].sudo()
        # Group lines by (employee_id, year, month) for batched search.
        by_key = {}
        for line in self:
            line.x_pending_pay_run_id = False
            if line.x_recover_from_run_id and line.state == 'pending':
                line.x_pending_pay_run_id = line.x_recover_from_run_id
                continue
            if not (line.x_awaiting_commission and line.state == 'pending'
                    and line.employee_id and line.year and line.month):
                continue
            try:
                period = fields.Date.to_date(
                    '%04d-%02d-01' % (line.year, line.month))
            except ValueError:
                continue
            by_key.setdefault(period, self.browse())
            by_key[period] |= line
        if not by_key:
            return
        # One search per distinct period — the run is per month, not per
        # employee, so this is a handful of queries at most.
        for period, lines in by_key.items():
            run = Run.search([('period', '=', period)], limit=1)
            for line in lines:
                line.x_pending_pay_run_id = run.id or False

    # ------------------------------------------------------------------
    # Settlement label override — show parked / commission-paid states.
    # ------------------------------------------------------------------
    @api.depends('x_awaiting_commission', 'x_paid_via_pay_run_line_id',
                 'x_paid_via_pay_run_line_id.display_name',
                 'x_recover_from_run_id',
                 'x_pending_pay_run_id',
                 'x_pending_pay_run_id.display_name')
    def _compute_settlement_label(self):
        super()._compute_settlement_label()
        for line in self:
            # 1) Already finalised by a commission sheet — overrides
            #    every other label, including the parent's "Manual
            #    by ..." since x_paid_via_pay_run_line_id is the
            #    authoritative settlement.
            # sudo(): the deduction owner names the run without having
            # access to commission records (same reason as the payslip
            # reference in KSW_deduction's own label).
            if line.x_paid_via_pay_run_line_id:
                run_name = (line.x_paid_via_pay_run_line_id.sudo()
                            .run_id.display_name or '')
                if line.x_recover_from_run_id:
                    line.settlement_label = _(
                        'Recovered from the %(run)s commission',
                        run=run_name)
                else:
                    line.settlement_label = _(
                        'Paid via the %s commission run', run_name)
                continue
            if line.x_recover_from_run_id and line.state == 'pending':
                line.settlement_label = _(
                    'Reserved for the %(run)s commission',
                    run=line.x_recover_from_run_id.sudo().display_name or '')
                continue
            # 2) Pending and parked for the commission — show the
            #    matching sheet ref if one exists, otherwise just
            #    flag it as awaiting.
            if line.x_awaiting_commission and line.state == 'pending':
                run = line.x_pending_pay_run_id.sudo()
                if run:
                    line.settlement_label = _(
                        'Awaiting the %s commission run',
                        run.display_name or '',
                    )
                else:
                    line.settlement_label = _(
                        'Awaiting the commission run (not yet created)',
                    )

    # ------------------------------------------------------------------
    # Create override — let the accountant add a PENDING
    # awaiting-commission sibling without the manual-paid forcing.
    # ------------------------------------------------------------------
    # The base ``KSW_deduction`` create override (in
    # ksw_deduction_line.py of that module) forces every non-auto
    # create to ``is_manual=True, state='paid'`` so it can only be
    # used for cash/bank manual entries. Adding a pending
    # awaiting-commission row needs a different path: still gated
    # behind ``group_installment_edit`` (just like manual entries),
    # but kept in ``state='pending'`` and ``is_manual=False`` so the
    # commission sheet can later flip it to paid.
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        # Detect any vals that explicitly request the awaiting-commission
        # path. Auto-generation (parent ``_generate_installment_lines``)
        # never sets ``x_awaiting_commission``, so this branch only
        # fires for genuine accountant-driven creates.
        flagged = [v for v in vals_list if v.get('x_awaiting_commission')]
        if not flagged or self.env.context.get('_ksw_auto_generating'):
            records = super().create(vals_list)
            records._refresh_pay_run_estimates()
            return records
        # Process the two groups separately so each goes through the
        # right code path on the parent.
        normal_vals = [v for v in vals_list if not v.get('x_awaiting_commission')]
        results = self.env['ksw.deduction.line']
        if normal_vals:
            results |= super().create(normal_vals)
        # Awaiting-commission rows: gate the privilege ourselves and
        # forward to super with ``_ksw_auto_generating=True`` so the
        # parent skips its manual-create gate (which would force
        # ``is_manual=True, state='paid'``).
        if flagged and not self.env.su:
            user = self.env.user
            if not user.has_group('KSW_deduction.group_installment_edit'):
                raise UserError(_(
                    "Adding an 'Awaiting Commission' installment "
                    "requires the 'Loan Installment Modification' "
                    "privilege. Ask an administrator to grant it."
                ))
        for v in flagged:
            v.setdefault('state', 'pending')
            v.setdefault('is_manual', False)
            # Stamp x_original_amount on creation for parity with
            # auto-generated lines (audit trail).
            if 'x_original_amount' not in v:
                v['x_original_amount'] = v.get('amount', 0.0)
            if v.get('payslip_id'):
                raise UserError(_(
                    "An 'Awaiting Commission' installment cannot be "
                    "linked to a payslip — it represents a slice "
                    "routed to the commission sheet."
                ))
        results |= super(KswDeductionLine, self.with_context(
            _ksw_auto_generating=True,
        )).create(flagged)
        results._refresh_pay_run_estimates()
        return results

    # ------------------------------------------------------------------
    # Keep an open month's Loans column in step with its installments.
    # ------------------------------------------------------------------
    _PAY_RUN_ESTIMATE_FIELDS = frozenset({
        'x_awaiting_commission', 'amount', 'year', 'month', 'state',
        'deduction_id', 'x_recover_from_run_id',
    })

    def _pay_run_estimate_keys(self):
        keys = {(l.employee_id.id, l.year, l.month) for l in self}
        # A reserved row weighs on the month it is recovered from, not the
        # month it is due in.
        keys |= {
            (l.employee_id.id, l.x_recover_from_run_id.sudo().period.year,
             l.x_recover_from_run_id.sudo().period.month)
            for l in self if l.x_recover_from_run_id
        }
        return keys

    def _refresh_pay_run_estimates(self, keys=None):
        if keys is None:
            keys = self._pay_run_estimate_keys()
        self.env['ksw.pay.run.line']._refresh_loan_estimates_for(keys)

    def write(self, vals):
        if not self._PAY_RUN_ESTIMATE_FIELDS.intersection(vals):
            return super().write(vals)
        # Before as well as after: a row moved from September to August
        # changes both months.
        keys = self._pay_run_estimate_keys()
        res = super().write(vals)
        self._refresh_pay_run_estimates(keys | self._pay_run_estimate_keys())
        return res

    def unlink(self):
        keys = self._pay_run_estimate_keys()
        res = super().unlink()
        self._refresh_pay_run_estimates(keys)
        return res
