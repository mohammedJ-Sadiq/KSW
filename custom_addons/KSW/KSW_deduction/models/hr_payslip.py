import logging

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)

# Order in which pending installments are collected: penalties / advances
# before loans (lower payroll_priority first), then oldest period, then
# sequence. Used both for input injection and for shortfall allocation.
_KSW_LINE_ORDER = 'payroll_priority, period_date, sequence'


class HrPayslip(models.Model):
    _inherit = 'hr.payslip'

    def compute_sheet(self):
        for payslip in self:
            self._inject_ksw_deduction_inputs(payslip)
        res = super().compute_sheet()
        # Second pass: if the salary can't cover every KSW deduction,
        # cap the input amounts by priority (so the net never goes
        # negative) and recompute. `super()` is used for the rerun so we
        # do not re-inject the full amounts.
        rerun = self.filtered(
            lambda s: s._ksw_apply_deduction_priority())
        if rerun:
            super(HrPayslip, rerun).compute_sheet()
        return res

    def _ksw_pending_lines_domain(self, employee_ids, date_to, full=False):
        """Pending installments collectable on a payslip ending `date_to`.

        No lower period bound: an installment left pending in an earlier
        month (because a past payslip could not afford it) is naturally
        picked up — i.e. the unaffordable remainder rolls forward.

        `full=True` drops the UPPER bound as well, so installments not yet
        due are pulled forward too. That is the vacation / EOS case: the
        settlement is the last (or the only) chance to collect. An EOS
        employee has no next payroll run at all, so anything left behind is
        never collected — which is why accounting had taken to re-typing the
        loan balance into "Other Deductions" by hand.
        """
        domain = [
            ('employee_id', 'in', employee_ids),
            ('state', '=', 'pending'),
            ('deduction_id.state', '=', 'active'),
        ]
        if not full:
            domain.append(('period_date', '<=', date_to))
        return domain

    @api.model
    def get_inputs(self, versions, date_from, date_to):
        """Extend base payslip input generation so pending KSW deduction
        installments appear in "Other Inputs" immediately upon payslip
        creation (i.e. at `onchange_employee` time), not only after
        Compute. `compute_sheet` still deletes + regenerates KSW_DED_*
        inputs, so this is just for the create/onchange path.
        """
        res = super().get_inputs(versions, date_from, date_to)
        if not versions or not date_from or not date_to:
            return res
        employees = versions.mapped('employee_id')
        if not employees:
            return res
        lines = self.env['ksw.deduction.line'].sudo().search(
            self._ksw_pending_lines_domain(employees.ids, date_to),
            order=_KSW_LINE_ORDER,
        )
        if not lines:
            return res
        # Map employee -> version (pick first matching version per employee)
        emp_version = {}
        for v in versions:
            emp_version.setdefault(v.employee_id.id, v.id)
        for line in lines:
            version_id = emp_version.get(line.employee_id.id)
            if not version_id:
                continue
            ded = line.deduction_id
            res.append({
                'name': '%s [%s] inst %d/%d' % (
                    ded.type_id.name, ded.name,
                    line.sequence, ded.installments),
                'code': 'KSW_DED_%d' % line.id,
                'amount': line.amount,
                'version_id': version_id,
            })
        return res

    def _inject_ksw_deduction_inputs(self, payslip):
        if not payslip.employee_id or not payslip.date_from or not payslip.date_to:
            return
        old = payslip.input_line_ids.filtered(
            lambda i: i.code and i.code.startswith('KSW_DED_'))
        if old:
            old.unlink()
        # A vacation / EOS payslip settles the WHOLE obligation, including
        # installments not yet due — see `_ksw_pending_lines_domain`.
        full = payslip._ksw_shows_full_deductions()
        lines = self.env['ksw.deduction.line'].sudo().search(
            self._ksw_pending_lines_domain(
                [payslip.employee_id.id], payslip.date_to, full=full),
            order=_KSW_LINE_ORDER,
        )
        if not lines:
            return
        version_id = (
            payslip.version_id.id
            or (payslip.employee_id.current_version_id
                and payslip.employee_id.current_version_id.id)
        )
        if not version_id:
            return
        InputLine = self.env['hr.payslip.input'].sudo()
        seq = 50
        for line in lines:
            ded = line.deduction_id
            label = '%s [%s] inst %d/%d' % (
                ded.type_id.name, ded.name, line.sequence, ded.installments)
            # Mark the ones pulled forward, so the reviewer can see at a
            # glance which lines this settlement is collecting early.
            if line.period_date and line.period_date > payslip.date_to:
                label += _(' — pulled forward, due %(period)s',
                           period=line.period_date.strftime('%m/%Y'))
            InputLine.create({
                'payslip_id': payslip.id,
                'version_id': version_id,
                'name': label,
                'code': 'KSW_DED_%d' % line.id,
                'amount': line.amount,
                'sequence': seq,
            })
            seq += 1

    def _ksw_shows_full_deductions(self):
        """Present the WHOLE pending obligation, even into a negative net?

        True for vacation / EOS payslips (those linked to a leave through
        ``hr.payslip.x_leave_id``).  Accounting reviews the employee's whole
        financial position at that moment, so a shortfall must be visible on
        the document rather than silently capped away.  On an ordinary
        monthly payslip the historical behaviour is kept: the inputs are
        capped so the net never goes negative.

        A **revision** is excluded even when it carries ``x_leave_id``
        (``_create_revision`` copies it): a revision is a payment document
        whose net IS the difference still owed, and
        ``_overpaid_revisions`` reads a negative net there as "we over-paid
        the employee" — ``_handle_overpaid_revisions`` then cancels the
        revision and opens a recovery deduction. A deduction shortfall must
        never be mistaken for an over-payment, so a revision keeps the
        capping behaviour.

        Either way only the affordable part is ever settled as paid — see
        ``_sync_deductions_on_done``.
        """
        self.ensure_one()
        return bool(self.x_leave_id) and not self.x_is_revision

    def _ksw_apply_deduction_priority(self):
        """Allocate the affordable pay across the pending KSW installments.

        Collects in priority order (penalties before loans, oldest period
        first).  Two presentation modes, one settlement rule:

        * ordinary payslip — the input `amount` is CAPPED to what the pay
          can absorb, so the net never goes negative.  Returns True when
          something was reduced, and the caller recomputes the payslip.
        * vacation / EOS payslip (`_ksw_shows_full_deductions`) — the input
          keeps its FULL amount so the whole obligation shows on the
          payslip and the net is allowed to go negative.  The unaffordable
          part is recorded in `x_ksw_uncollected` instead, and no recompute
          is needed (returns False).

        In both modes `amount - x_ksw_uncollected` is the amount actually
        consumed from this payslip, and it is the only part that is marked
        paid.
        """
        self.ensure_one()
        inputs = self.input_line_ids.filtered(
            lambda i: i.code and i.code.startswith('KSW_DED_')
            and i.code[8:].isdigit())
        if not inputs:
            return False
        cur = self.company_id.currency_id or self.env.company.currency_id
        ksw_total = sum(inputs.mapped('amount'))
        # Net was computed in pass 1 with the full KSW deductions applied;
        # adding them back gives the salary available before KSW.
        net = self.get_salary_line_total('NET')
        available = net + ksw_total
        if cur.compare_amounts(available, ksw_total) >= 0:
            # Everything affordable: no capping, nothing carried forward.
            stale = inputs.filtered(lambda i: i.x_ksw_uncollected)
            if stale:
                stale.write({'x_ksw_uncollected': 0.0})
            return False

        # Order inputs by the underlying line's priority (penalties first).
        prio = {}
        for inp in inputs:
            line = self.env['ksw.deduction.line'].browse(int(inp.code[8:]))
            prio[inp.id] = (
                line.payroll_priority, line.period_date or fields.Date.today(),
                line.sequence)
        ordered = inputs.sorted(key=lambda i: prio[i.id])

        full_view = self._ksw_shows_full_deductions()
        remaining = max(available, 0.0)
        changed = False
        for inp in ordered:
            full = inp.amount
            alloc = cur.round(min(full, remaining))
            if full_view:
                # Keep the obligation on the document; record the shortfall.
                uncollected = cur.round(full - alloc)
                if cur.compare_amounts(
                        inp.x_ksw_uncollected, uncollected) != 0:
                    inp.x_ksw_uncollected = uncollected
            else:
                if inp.x_ksw_uncollected:
                    inp.x_ksw_uncollected = 0.0
                if cur.compare_amounts(alloc, full) != 0:
                    inp.amount = alloc
                    changed = True
            remaining = max(remaining - alloc, 0.0)
        return changed

    # ------------------------------------------------------------------
    # Deduction coverage summary (payslip form + leave accounting page)
    # ------------------------------------------------------------------
    # None of these carry a model-level ``groups=``: they are referenced in
    # ``invisible=`` on view elements shown to leave approvers who are not
    # payroll users, which would trip the OWL "field is undefined" crash
    # (pitfall #31).  The computes read restricted data through ``sudo()``
    # and visibility is controlled view-side only.

    x_ksw_ded_presented = fields.Float(
        string='Deductions Presented', digits=(16, 2),
        compute='_compute_ksw_deduction_coverage',
        help='Total of the KSW deduction installments shown on this '
             'payslip.',
    )
    x_ksw_ded_collected = fields.Float(
        string='Collected by This Payslip', digits=(16, 2),
        compute='_compute_ksw_deduction_coverage',
        help='The part of the presented deductions the pay could actually '
             'absorb. Only this part is settled as paid.',
    )
    x_ksw_ded_carried = fields.Float(
        string='Not Collected (Still Pending)', digits=(16, 2),
        compute='_compute_ksw_deduction_coverage',
        help='The part the pay could not cover. It stays pending on the '
             'deduction schedule and is collected by a later payroll run.',
    )
    x_ksw_ded_outstanding = fields.Float(
        string='Employee Total Outstanding', digits=(16, 2),
        compute='_compute_ksw_deduction_coverage',
        help='Every still-pending installment the employee owes across all '
             'active deductions, whatever month it falls in.',
    )

    @api.depends('employee_id', 'input_line_ids.code', 'input_line_ids.amount',
                 'input_line_ids.x_ksw_uncollected')
    def _compute_ksw_deduction_coverage(self):
        for slip in self:
            inputs = slip.input_line_ids.filtered(
                lambda i: i.code and i.code.startswith('KSW_DED_')
                and i.code[8:].isdigit())
            presented = sum(inputs.mapped('amount'))
            carried = sum(i.x_ksw_uncollected or 0.0 for i in inputs)
            slip.x_ksw_ded_presented = presented
            slip.x_ksw_ded_carried = carried
            slip.x_ksw_ded_collected = presented - carried
            # sudo(): x_deduction_outstanding_total is gated behind
            # hr.group_hr_user, and the loan/accounting approvers who read
            # this summary are not necessarily HR users.
            slip.x_ksw_ded_outstanding = (
                slip.employee_id.sudo().x_deduction_outstanding_total
                if slip.employee_id else 0.0)

    def write(self, vals):
        new_state = vals.get('state')
        prev = {s.id: s.state for s in self} if new_state else {}
        result = super().write(vals)
        if new_state:
            for slip in self:
                old = prev.get(slip.id)
                if new_state == 'done' and old != 'done':
                    self._sync_deductions_on_done(slip)
                elif new_state in ('draft', 'cancel') and old == 'done':
                    self._sync_deductions_on_reset(slip)
        return result

    def _sync_deductions_on_done(self, payslip):
        """Reconcile deduction lines with what the payslip actually
        collected — `amount - x_ksw_uncollected` per KSW_DED_* input
        (after `_ksw_apply_deduction_priority`):
          • full          → mark the line paid;
          • zero           → leave it pending (rolls forward next month);
          • partial        → split: pay the affordable part, forward the
                              remainder as a new pending line.
        """
        Line = self.env['ksw.deduction.line'].sudo()
        alloc = {}  # line_id -> collected amount
        for i in payslip.input_line_ids:
            if i.code and i.code.startswith('KSW_DED_') and i.code[8:].isdigit():
                # On a vacation / EOS payslip the input carries the FULL
                # obligation for visibility; what the pay actually absorbed
                # is `amount - x_ksw_uncollected`. On an ordinary payslip
                # (and on every payslip computed before that field existed)
                # x_ksw_uncollected is 0, so this is the capped amount as
                # before. Never settle money the payslip did not pay.
                alloc[int(i.code[8:])] = i.amount - (i.x_ksw_uncollected or 0.0)
        if not alloc:
            return
        self.env['ksw.deduction'].sudo()._settle_payslip_lines(
            Line.browse(list(alloc)), alloc, payslip)

    def _sync_deductions_on_reset(self, payslip):
        self.env['ksw.deduction'].sudo()._unmark_lines_paid(payslip)

    # ------------------------------------------------------------------
    # Revision support (hooks defined in KSW_payroll)
    # ------------------------------------------------------------------

    def _revision_frozen_deduction_inputs(self, revision, prior_slips,
                                          version_id):
        """Reproduce, on a revision, the installments the payslip(s) it
        supersedes already collected.

        A revision re-states the whole period, so the deserved net must
        include the deductions that were legitimately taken — otherwise the
        difference comes out too high by exactly the amount collected.

        The code prefix is `KSW_DEDP_`, NOT `KSW_DED_`. That single letter
        is load-bearing: `_inject_ksw_deduction_inputs`,
        `_ksw_apply_deduction_priority` and `_sync_deductions_on_done` all
        test `startswith('KSW_DED_')`, which `'KSW_DEDP_5'` fails — so these
        rows are invisible to the injector, to the shortfall capper and to
        the settlement pass. The KSW_DEDUCTIONS salary rule sums the shorter
        `'KSW_DED'` prefix, so they still reach the payslip total. Net
        effect: they count towards the deserved net and the deduction ledger
        is left untouched by the revision. Installments still *pending* are
        picked up normally as `KSW_DED_` and are collected out of the
        difference.
        """
        collected = self.env['ksw.deduction.line'].sudo().search([
            ('payslip_id', 'in', prior_slips.ids),
            ('state', '=', 'paid'),
        ])
        vals = []
        seq = 90
        for line in collected:
            ded = line.deduction_id
            vals.append({
                'payslip_id': revision.id,
                'version_id': version_id,
                'name': _(
                    '%(type)s [%(ref)s] inst %(n)s/%(total)s — already '
                    'collected in %(slip)s',
                    type=ded.type_id.name, ref=ded.name,
                    n=line.sequence, total=ded.installments,
                    slip=(line.payslip_id.number
                          or line.payslip_id.display_name)),
                'code': 'KSW_DEDP_%d' % line.id,
                'amount': line.amount,
                'sequence': seq,
            })
            seq += 1
        return vals

    # ------------------------------------------------------------------
    # Over-payment recovery (hook defined in KSW_payroll)
    # ------------------------------------------------------------------

    def _create_overpayment_deduction(self, amount):
        """Open a draft deduction recovering an over-paid revision.

        A revision whose recomputed period comes out *below* what was
        already paid cannot be confirmed — a payslip has no way to pay a
        negative amount, and the bank export drops negative-NET rows
        silently. Instead the revision is cancelled and the difference is
        scheduled for recovery from a later payroll run.

        `sudo()` is required, not merely convenient: `ksw.deduction.create()`
        calls `_check_acc_data_entry_ownership()`, which rejects a payroll
        officer creating an accounting-data-entry type. Authorisation was
        already established by `hr.payslip._check_payroll_officer` on the
        way in.
        """
        self.ensure_one()
        ded_type = self._overpayment_deduction_type()
        if not ded_type:
            _logger.warning(
                'No over-payment recovery deduction type configured — '
                'revision %s cancelled without a recovery deduction.',
                self.id)
            return self.env['ksw.deduction']
        source = self.x_revised_payslip_id
        return self.env['ksw.deduction'].sudo().create({
            'employee_id': self.employee_id.id,
            'type_id': ded_type.id,
            'amount': amount,
            'installments': 1,
            'start_month': fields.Date.context_today(self).replace(day=1),
            'reason': _('Salary over-payment for %(date_from)s → %(date_to)s',
                        date_from=self.date_from, date_to=self.date_to),
            'description': _(
                'Automatically opened from payslip revision %(revision)s '
                '(revision of %(source)s). Recomputing the period with '
                'current data produced a net %(amount).2f lower than the '
                'amount already paid to the employee.',
                revision=self.name or self.id,
                source=(source.number or source.name or '') if source else '',
                amount=amount),
        })

    def _overpayment_deduction_type(self):
        """Configured recovery type, defaulting to Salary Advance."""
        param = self.env['ir.config_parameter'].sudo().get_param(
            'ksw_payroll.overpay_recovery_type_id')
        DedType = self.env['ksw.deduction.type'].sudo()
        if param:
            ded_type = DedType.browse(int(param)).exists()
            if ded_type:
                return ded_type
        return self.env.ref(
            'KSW_deduction.type_advance',
            raise_if_not_found=False) or DedType.browse()
