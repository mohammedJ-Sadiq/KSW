from dateutil.relativedelta import relativedelta
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class KswDeductionRescheduleWizard(models.TransientModel):
    """Rebuild the *remaining* repayment plan of one or more deductions.

    The gap this fills
    ------------------
    On an active deduction the Installments tab lets a privileged user
    re-value and postpone the lines that already exist, but it can never
    *lengthen* a plan: ``ksw.deduction.line.create()`` forces any row added
    by hand to ``is_manual=True, state='paid'`` (it is the manual-payment
    entry point), and ``unlink()`` refuses to delete an auto-generated line
    outside the cancel / reset-to-draft flow. So "spread what is left over
    six months instead of four" had no route at all, and the only apparent
    alternative — cancelling two loans and re-issuing them as one — is a
    novation: it write-offs (``x_writeoff_date``) debts that were never
    forgiven, re-dates the whole balance to today on the Statement of
    Account, records a disbursement that never happened, and orphans the
    installments already collected against real payslips.

    Rescheduling keeps each contract whole. Only the plan changes.

    What it does
    ------------
    Per deduction, the **outstanding** balance (the sum of its ``pending``
    lines) is re-spread over ``installments`` months from ``start_month``.
    Lines already ``paid`` are never touched, so the grand total is
    unchanged and ``_validate_installments_total`` still balances — that is
    what makes this safe to run on a half-collected loan.

    Why it takes several deductions at once
    ---------------------------------------
    "1,500 a month for six months" is a statement about the *employee's*
    total monthly deduction, not about any one loan. Rescheduling loan by
    loan means splitting that figure by hand and nothing afterwards holds
    the sum to it. Selecting the employee's whole outstanding book instead
    spreads each deduction's own balance over the same window, so the
    monthly total lands on the target pro-rata and each loan keeps its own
    reference, chatter and ledger.

    Known consequence
    -----------------
    Old pending lines are replaced, so a pending remainder that a payslip
    shortfall had forwarded (``forwarded_from_payslip_id`` /
    ``split_origin_id``) loses its back-reference and resetting that old
    payslip to draft will no longer merge it back. Superseding the previous
    plan is the point of the operation; the chatter records what it was.
    """

    _name = 'ksw.deduction.reschedule.wizard'
    _description = 'Reschedule Deduction Installments'

    # ── Target ───────────────────────────────────────────────────────
    deduction_id = fields.Many2one(
        'ksw.deduction', string='Deduction', required=True, readonly=True,
        ondelete='cascade',
    )
    employee_id = fields.Many2one(
        related='deduction_id.employee_id', readonly=True,
    )
    currency_id = fields.Many2one(
        related='deduction_id.currency_id', readonly=True,
    )
    # Other active deductions of the same employee that this user is
    # allowed to touch. Never offer a picker wider than the caller's
    # authority: `_selectable_siblings` filters on the same
    # `x_can_edit_installments` matrix that guards the confirm.
    sibling_ids = fields.Many2many(
        'ksw.deduction', 'ksw_resched_wiz_sibling_rel', 'wizard_id', 'ded_id',
        string='Also reschedule',
        compute='_compute_sibling_ids', readonly=False, store=True,
        help='Other active deductions of this employee with a balance left. '
             'Include them to spread the whole outstanding book over the '
             'same window — the monthly total then lands on the target '
             'pro-rata instead of having to be split by hand.',
    )
    available_sibling_ids = fields.Many2many(
        'ksw.deduction', 'ksw_resched_wiz_avail_rel', 'wizard_id', 'ded_id',
        compute='_compute_available_siblings',
        help='Technical: domain source for sibling_ids.',
    )
    has_siblings = fields.Boolean(compute='_compute_available_siblings')

    # ── Plan ─────────────────────────────────────────────────────────
    plan_by = fields.Selection(
        [
            ('months', 'Number of months'),
            ('monthly', 'Monthly amount'),
        ],
        string='Plan by', default='months', required=True,
        help='Enter whichever of the two you decided on — the other is '
             'derived and shown below, so you can check both before '
             'confirming.',
    )
    installments = fields.Integer(
        string='Number of Months', default=6,
        help='How many monthly installments the remaining balance is spread '
             'over.',
    )
    target_monthly = fields.Monetary(
        string='Monthly Amount',
        help='Total to deduct from this employee each month across every '
             'selected deduction. The number of months is derived from it.',
    )
    start_month = fields.Date(
        string='First Installment', required=True,
        default=lambda s: fields.Date.context_today(s).replace(day=1),
        help='Month the new plan starts. Any day is accepted — only the '
             'month is used. A month already in the past is still collected '
             'by the next payslip, so backdating is harmless.',
    )
    reason = fields.Char(
        string='Reason', required=True,
        help='Why the plan is being changed. Recorded in the chatter of '
             'every deduction rescheduled.',
    )

    # ── Preview (non-stored computes) ────────────────────────────────
    total_outstanding = fields.Monetary(
        string='Total Outstanding', compute='_compute_preview',
    )
    effective_installments = fields.Integer(
        string='Months', compute='_compute_preview',
    )
    monthly_total = fields.Monetary(
        string='Monthly Total', compute='_compute_preview',
    )
    end_month = fields.Date(
        string='Last Installment', compute='_compute_preview',
    )
    gross_salary = fields.Monetary(
        string='Gross Salary', compute='_compute_preview',
    )
    # A 0..1 ratio: the `percentage` widget in the form multiplies by 100.
    pct_of_gross = fields.Float(
        string='% of Gross', compute='_compute_preview',
        help='Share of the monthly gross salary this plan withholds.',
    )
    allocation_html = fields.Html(
        string='Allocation', compute='_compute_preview', sanitize=False,
    )

    # ==================================================================
    # Helpers
    # ==================================================================

    @api.model
    def _selectable_siblings(self, deduction):
        """Active deductions of the same employee, minus this one, that
        still have a balance AND that the current user may reschedule.

        `x_can_edit_installments` is the same per-record matrix the
        Installments tab and the payment wizard use, so a user is never
        shown a record the confirm would then refuse.
        """
        if not deduction.employee_id:
            return self.env['ksw.deduction'].browse()
        siblings = self.env['ksw.deduction'].search([
            ('employee_id', '=', deduction.employee_id.id),
            ('state', '=', 'active'),
            ('id', '!=', deduction.id),
        ])
        return siblings.filtered(
            lambda d: d.x_can_edit_installments
            and any(l.state == 'pending' for l in d.line_ids)
        )

    def _target_deductions(self):
        """Every deduction this wizard will rewrite."""
        self.ensure_one()
        return self.deduction_id | self.sibling_ids

    @staticmethod
    def _pending(deduction):
        return deduction.line_ids.filtered(lambda l: l.state == 'pending')

    def _outstanding_by_deduction(self):
        """{deduction: outstanding} for the current selection."""
        self.ensure_one()
        return {
            ded: sum(self._pending(ded).mapped('amount'))
            for ded in self._target_deductions()
        }

    def _resolve_installments(self, outstanding):
        """The month count the plan actually uses.

        In ``monthly`` mode it is derived from the target: enough whole
        months to clear the balance, i.e. ``ceil(outstanding / target)``.
        The final month then collects the remainder, which is *less* than
        the target — never more.
        """
        self.ensure_one()
        if self.plan_by == 'monthly':
            target = self.target_monthly or 0.0
            if target <= 0 or outstanding <= 0:
                return 0
            cur = self.deduction_id.currency_id
            n = int(outstanding // target)
            if cur.compare_amounts(n * target, outstanding) < 0:
                n += 1
            return max(n, 1)
        return self.installments or 0

    def _build_schedule(self, outstanding, n_months):
        """Split ``outstanding`` into ``n_months`` amounts.

        Each is ``outstanding / n`` rounded to 2 dp; the last absorbs the
        residue so the sum is exact — the same rule as
        ``ksw.deduction._generate_installment_lines``, so a rescheduled
        plan is indistinguishable from a freshly generated one.
        """
        if n_months < 1 or outstanding <= 0:
            return []
        per = round(outstanding / n_months, 2)
        amounts, running = [], 0.0
        for i in range(n_months):
            if i < n_months - 1:
                amounts.append(per)
                running += per
            else:
                amounts.append(round(outstanding - running, 2))
        return amounts

    # ==================================================================
    # Computes
    # ==================================================================

    @api.depends('deduction_id')
    def _compute_sibling_ids(self):
        """Preselect every sibling the caller may touch.

        The case this wizard was built for is precisely an employee
        carrying more than one balance that has to come down to a single
        monthly figure, so including them is the useful default. Editable
        (`readonly=False`): unticking one reschedules that contract alone.
        """
        for wiz in self:
            wiz.sibling_ids = [
                (6, 0, wiz._selectable_siblings(wiz.deduction_id).ids)]

    @api.depends('deduction_id')
    def _compute_available_siblings(self):
        for wiz in self:
            siblings = wiz._selectable_siblings(wiz.deduction_id)
            wiz.available_sibling_ids = [(6, 0, siblings.ids)]
            wiz.has_siblings = bool(siblings)

    @api.depends('deduction_id', 'sibling_ids', 'plan_by', 'installments',
                 'target_monthly', 'start_month')
    def _compute_preview(self):
        for wiz in self:
            by_ded = wiz._outstanding_by_deduction()
            outstanding = sum(by_ded.values())
            n = wiz._resolve_installments(outstanding)
            wiz.total_outstanding = outstanding
            wiz.effective_installments = n
            wiz.monthly_total = round(outstanding / n, 2) if n else 0.0
            start = wiz.start_month
            wiz.end_month = (
                start.replace(day=1) + relativedelta(months=n - 1)
                if start and n else False
            )
            # sudo(): `wage` is group-restricted (Odoo 19 Pitfalls #5) and
            # this wizard is opened by accounting roles that do not hold
            # the HR groups. No model-level groups= on the output field
            # (pitfall #31) — the sudo IS the protection.
            emp = wiz.employee_id
            gross = emp.sudo().current_version_id.wage if emp else 0.0
            wiz.gross_salary = gross
            wiz.pct_of_gross = (wiz.monthly_total / gross) if gross else 0.0
            wiz.allocation_html = wiz._render_allocation(by_ded, n)

    def _render_allocation(self, by_ded, n_months):
        """Per-deduction breakdown of the proposed plan.

        Shown because a monthly total is only trustworthy once you can see
        which contract each riyal of it belongs to.
        """
        self.ensure_one()
        if not n_months or not by_ded:
            return False
        cur = self.deduction_id.currency_id.name or ''
        rows = Markup('')
        for ded, outstanding in by_ded.items():
            amounts = self._build_schedule(outstanding, n_months)
            if not amounts:
                continue
            first, last = amounts[0], amounts[-1]
            tail = (
                Markup(' <span class="text-muted">(last: %(l).2f)</span>')
                % {'l': last}
                if abs(last - first) >= 0.01 else Markup('')
            )
            rows += Markup(
                '<tr><td>%(name)s</td><td class="text-muted">%(type)s</td>'
                '<td class="text-end">%(out).2f</td>'
                '<td class="text-end"><b>%(per).2f</b>%(tail)s</td></tr>'
            ) % {
                'name': ded.name or '',
                'type': ded.type_id.name or '',
                'out': outstanding,
                'per': first,
                'tail': tail,
            }
        return Markup(
            '<table class="table table-sm mb-0">'
            '<thead><tr><th>Deduction</th><th>Type</th>'
            '<th class="text-end">Outstanding (%(cur)s)</th>'
            '<th class="text-end">Per Month (%(cur)s)</th></tr></thead>'
            '<tbody>%(rows)s</tbody></table>'
        ) % {'cur': cur, 'rows': rows}

    # ==================================================================
    # Confirm
    # ==================================================================

    def action_confirm(self):
        self.ensure_one()
        targets = self._target_deductions()

        # ── Authority ────────────────────────────────────────────────
        # Same per-record matrix as the Installments tab and the payment
        # wizard: "Loan Modification: Full" reschedules any type, the
        # Accounting Data Entry role reschedules the non-loan types only.
        if not self.env.su:
            for ded in targets:
                if not ded.x_can_edit_installments:
                    raise UserError(_(
                        "You are not allowed to reschedule %(name)s. "
                        "Accounting staff with the 'Loan Modification: "
                        "Full' privilege can reschedule any type; the "
                        "Accounting Data Entry role can reschedule non-loan "
                        "deductions (salary advances and penalties) only.",
                        name=ded.display_name,
                    ))

        # ── Input ────────────────────────────────────────────────────
        if any(d.state != 'active' for d in targets):
            raise UserError(_(
                "Only active deductions can be rescheduled."))
        employees = targets.mapped('employee_id')
        if len(employees) > 1:
            raise UserError(_(
                "All selected deductions must belong to the same employee."))

        by_ded = self._outstanding_by_deduction()
        outstanding = sum(by_ded.values())
        if outstanding <= 0:
            raise UserError(_(
                "The selected deductions have no pending installments left "
                "to reschedule."))

        n_months = self._resolve_installments(outstanding)
        if n_months < 1:
            raise ValidationError(_(
                "Enter a number of months (or a monthly amount) greater "
                "than zero."))
        if n_months > 120:
            raise ValidationError(_(
                "A repayment plan cannot exceed 120 months — you entered "
                "%(n)d. Check the monthly amount.", n=n_months))

        start = self.start_month.replace(day=1)
        reason = (self.reason or '').strip()
        user = self.env.user
        today = fields.Date.context_today(self)

        # ── Rewrite every schedule first, refresh payslips after ─────
        # Both loops run over the same set on purpose: recomputing a draft
        # payslip before the *other* deduction has been rescheduled would
        # value it against a half-applied plan and post a chatter note for
        # a NET that is about to change again.
        summaries = []
        for ded in targets:
            pending = self._pending(ded)
            ded_outstanding = by_ded[ded]
            amounts = self._build_schedule(ded_outstanding, n_months)
            if not amounts:
                continue
            paid = ded.line_ids.filtered(lambda l: l.state == 'paid')
            max_seq = max(paid.mapped('sequence') or [0])

            old_count = len(pending)
            old_months = ''
            if pending:
                ordered = pending.sorted(lambda l: (l.year, l.month))
                old_months = '%s → %s' % (
                    ordered[0].display_name, ordered[-1].display_name)

            commands = [(2, line.id) for line in pending]
            for i, amount in enumerate(amounts):
                period = start + relativedelta(months=i)
                commands.append((0, 0, {
                    'sequence': max_seq + i + 1,
                    'year': period.year,
                    'month': period.month,
                    'amount': amount,
                }))

            # `_ksw_auto_generating` marks these as schedule lines so
            # `ksw.deduction.line.create()` leaves them pending instead of
            # stamping them manual/paid. sudo() clears the line-level
            # unlink guard on auto lines — authority was checked above,
            # which is what makes it safe here (CLAUDE.md #11).
            # One parent write: `ksw.deduction.write()` mutes the per-line
            # total constraint for the duration and validates once at the
            # end, on the complete new plan.
            ded.sudo().with_context(_ksw_auto_generating=True).write({
                'line_ids': commands,
                'installments': len(paid) + n_months,
            })

            summaries.append((ded, old_count, old_months, amounts))

        # ── Chatter ──────────────────────────────────────────────────
        monthly_total = sum(s[3][0] for s in summaries)
        for ded, old_count, old_months, amounts in summaries:
            others = targets - ded
            shared = (
                Markup(
                    '<b>Rescheduled together with:</b> %(o)s<br/>'
                    '<b>Combined monthly:</b> %(mt).2f %(cur)s<br/>'
                ) % {
                    'o': ', '.join(others.mapped('name')),
                    'mt': monthly_total,
                    'cur': ded.currency_id.name or '',
                }
                if others else Markup('')
            )
            body = Markup(
                '<strong>📅 Installments Rescheduled</strong><br/>'
                '<b>By:</b> %(u)s on %(d)s<br/>'
                '<b>Reason:</b> %(r)s<br/>'
                '<b>Outstanding re-spread:</b> %(out).2f %(cur)s<br/>'
                '<b>Previous plan:</b> %(oc)d pending installment(s) '
                '%(om)s<br/>'
                '<b>New plan:</b> %(n)d × %(per).2f %(cur)s from '
                '%(start)s<br/>'
                '%(shared)s'
                '<span class="text-muted">Paid installments were not '
                'touched; the total amount of this deduction is '
                'unchanged.</span>'
            ) % {
                'u': user.name,
                'd': fields.Date.to_string(today),
                'r': reason,
                'out': sum(amounts),
                'cur': ded.currency_id.name or '',
                'oc': old_count,
                'om': '(%s)' % old_months if old_months else '',
                'n': len(amounts),
                'per': amounts[0],
                'start': start.strftime('%B %Y'),
                'shared': shared,
            }
            ded.sudo().message_post(body=body, subtype_xmlid='mail.mt_note')

        # ── Pull the new figures into any draft payslip ──────────────
        # A deduction that is already active has normally been injected
        # into this month's draft batch at the old amount. Without this
        # the bank file would still carry the superseded installment —
        # the same window `_refresh_draft_payslips` was written to close.
        for ded, _old_count, _old_months, _amounts in summaries:
            ded.sudo()._refresh_draft_payslips()

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'ksw.deduction',
            'res_id': self.deduction_id.id,
            'view_mode': 'form',
            'target': 'current',
        }
