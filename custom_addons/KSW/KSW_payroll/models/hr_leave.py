import calendar
import logging
import re
from datetime import date, timedelta

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


def _plain_text(html):
    """Tag-stripped, whitespace-collapsed text of an HTML body."""
    return ' '.join(re.sub(r'<[^>]+>', ' ', str(html or '')).split())

# Approval steps at which an approver may ask for a provisional
# ("draft / incomplete") vacation calculation.  Everything from the
# moment the request is created up to — but not including — the HR
# confirmation step, where the definitive payslip already exists.
PREVIEW_STATES = (
    'pending_dm',
    'pending_hr',
    'pending_gm_initial',
    'pending_acc',
    'pending_gm_final',
)

# Groups allowed to trigger and read a provisional calculation.
PREVIEW_GROUPS = (
    'om_hr_payroll.group_hr_payroll_user',
    'KSW_annual_leave.group_annual_leave_hr',
    'KSW_annual_leave.group_annual_leave_acc',
    'KSW_annual_leave.group_annual_leave_gm',
)


class HrLeave(models.Model):
    _inherit = 'hr.leave'

    # ------------------------------------------------------------------
    # Vacation payslip link (lives here because hr.payslip model
    # comes from om_hr_payroll which only KSW_payroll depends on).
    # ------------------------------------------------------------------

    x_vacation_payslip_id = fields.Many2one(
        'hr.payslip', string='Vacation Payslip', readonly=True, copy=False,
        # Annual-leave HR approvers can also see the link (needed by the
        # Recompute Payslip button, which they are authorised to use) —
        # keeping the field payroll-only while the button is visible to
        # HR crashes the form for HR users ("field is undefined").
        groups='om_hr_payroll.group_hr_payroll_user,'
               'KSW_annual_leave.group_annual_leave_hr',
        help='The vacation payslip generated for this annual leave '
             '(covers the current month at the time of approval).',
    )

    x_vacation_payslip_ids = fields.One2many(
        'hr.payslip', 'x_leave_id', string='Vacation Payslips',
        readonly=True, copy=False,
        groups='om_hr_payroll.group_hr_payroll_user',
        help='Vacation payslip(s) linked to this leave via x_leave_id.',
    )

    # ------------------------------------------------------------------
    # Provisional ("draft / incomplete") vacation calculation
    # ------------------------------------------------------------------
    # HR and Accounting routinely need the vacation-balance and salary
    # figures long before the GM signs off.  The fields below expose a
    # read-only summary of whichever payslip is currently attached to the
    # leave — a provisional one produced by action_preview_vacation_payslip
    # or the definitive one created at GM final approval.
    #
    # None of them carry a model-level ``groups=``: they are all computed
    # through ``sudo()`` (the underlying payslip and wage data is
    # group-restricted) and some are referenced in ``invisible=``
    # expressions on view elements, which would otherwise crash the form
    # for users outside that group (Odoo 19 OWL "field is undefined").
    # Visibility is controlled with view-level ``groups=`` instead.
    # ------------------------------------------------------------------

    x_can_preview_vacation_payslip = fields.Boolean(
        string='Can Preview Vacation Calculation',
        compute='_compute_vacation_calc_summary',
        help='True when the current user may generate a provisional '
             'vacation calculation at the current approval step.',
    )
    x_has_vacation_calc = fields.Boolean(
        string='Has Vacation Calculation',
        compute='_compute_vacation_calc_summary',
    )
    x_vacation_calc_is_preview = fields.Boolean(
        string='Calculation Is Provisional',
        compute='_compute_vacation_calc_summary',
        help='The figures below come from a provisional calculation and '
             'will be recomputed when the approval chain completes.',
    )
    x_vacation_calc_period = fields.Char(
        string='Calculation Period',
        compute='_compute_vacation_calc_summary',
    )
    x_vacation_calc_balance = fields.Float(
        string='Vacation Balance Settlement', digits=(16, 2),
        compute='_compute_vacation_calc_summary',
        help='The VACATION_BAL line — the monetary value of the annual '
             'leave balance being settled.',
    )
    x_vacation_calc_gross = fields.Float(
        string='Gross', digits=(16, 2),
        compute='_compute_vacation_calc_summary',
    )
    x_vacation_calc_deductions = fields.Float(
        string='Total Deductions', digits=(16, 2),
        compute='_compute_vacation_calc_summary',
    )
    x_vacation_calc_net = fields.Float(
        string='Net Payable', digits=(16, 2),
        compute='_compute_vacation_calc_summary',
    )

    def _current_vacation_payslip(self):
        """Return the live (non-cancelled) payslip attached to this leave.

        Covers both the vacation payslip and the EOS payslip — both are
        linked through ``hr.payslip.x_leave_id``.  The most recently
        created one wins, so a fresh recompute always shadows an older
        provisional run.
        """
        self.ensure_one()
        slips = self.sudo().x_vacation_payslip_ids.filtered(
            lambda p: p.state != 'cancel'
        )
        if not slips:
            return self.env['hr.payslip']
        return max(slips, key=lambda p: p.id)

    @staticmethod
    def _payslip_line_total(payslip, code):
        return sum(
            payslip.line_ids.filtered(lambda l: l.code == code).mapped('total')
        )

    @api.depends_context('uid')
    @api.depends('x_annual_approval_state', 'x_vacation_payslip_ids',
                 'x_vacation_payslip_ids.state',
                 'x_vacation_payslip_ids.line_ids.total')
    def _compute_vacation_calc_summary(self):
        allowed = self.env.su or any(
            self.env.user.has_group(g) for g in PREVIEW_GROUPS
        )
        for leave in self:
            leave.x_can_preview_vacation_payslip = bool(
                allowed
                and leave.x_annual_approval_state in PREVIEW_STATES
            )

            payslip = leave._current_vacation_payslip()
            leave.x_has_vacation_calc = bool(payslip)
            if not payslip:
                leave.x_vacation_calc_is_preview = False
                leave.x_vacation_calc_period = False
                leave.x_vacation_calc_balance = 0.0
                leave.x_vacation_calc_gross = 0.0
                leave.x_vacation_calc_deductions = 0.0
                leave.x_vacation_calc_net = 0.0
                continue

            gross = self._payslip_line_total(payslip, 'GROSS')
            net = self._payslip_line_total(payslip, 'NET')

            leave.x_vacation_calc_is_preview = payslip.x_is_vacation_preview
            leave.x_vacation_calc_period = '%s → %s' % (
                payslip.date_from, payslip.date_to)
            leave.x_vacation_calc_balance = self._payslip_line_total(
                payslip, 'VACATION_BAL')
            leave.x_vacation_calc_gross = gross
            # Derived rather than summed so the panel is always
            # arithmetically consistent with the stored NET (see the
            # integer-rounding note in KSW_payroll/models/hr_payslip.py).
            leave.x_vacation_calc_deductions = net - gross
            leave.x_vacation_calc_net = net

    def action_preview_vacation_payslip(self):
        """Generate a provisional vacation calculation for this leave.

        Produces the very same draft payslip that GM final approval would
        produce, flagged ``x_is_vacation_preview`` so it is obvious that
        the inputs are not final.  Approvers use it to see the vacation
        balance and salary figures before the chain completes.

        Any earlier provisional payslip for the leave is cancelled first,
        so there is never more than one live provisional run.
        """
        self.ensure_one()
        if not self.env.su and not any(
            self.env.user.has_group(g) for g in PREVIEW_GROUPS
        ):
            raise UserError(
                'Only HR, Accounting, GM approvers or Payroll users can '
                'generate a provisional vacation calculation.')
        if self.x_annual_approval_state not in PREVIEW_STATES:
            raise UserError(
                'A provisional calculation can only be generated while the '
                'request is still going through the approval chain.')

        # sudo(): authorisation was checked above; HR / Accounting / GM
        # approvers have no payroll ACLs of their own.
        leave = self.sudo()
        leave._cancel_preview_vacation_payslips()
        leave._create_vacation_payslip(preview=True)

        if not leave._current_vacation_payslip():
            raise UserError(
                'No provisional calculation could be produced for %s — the '
                'employee has no active contract version or no salary '
                'structure.' % self.employee_id.name)

        leave.message_post(
            body=Markup(
                '<strong>🧮 Provisional Vacation Calculation</strong><br/>'
                '<b>Generated by:</b> %(user)s<br/>'
                '<b>Note:</b> Draft figures — the request has not completed '
                'its approval chain yet.'
            ) % {'user': self.env.user.name},
            subtype_xmlid='mail.mt_note',
        )
        return {'type': 'ir.actions.client', 'tag': 'soft_reload'}

    def _cancel_preview_vacation_payslips(self):
        """Cancel provisional payslips so a definitive one can replace them."""
        for leave in self:
            previews = leave.sudo().x_vacation_payslip_ids.filtered(
                lambda p: p.x_is_vacation_preview and p.state != 'cancel'
            )
            if previews:
                previews.sudo().write({'state': 'cancel'})

    # ------------------------------------------------------------------
    # Override the hook defined in KSW_annual_leave to create a
    # vacation payslip when the GM gives final approval.
    # ------------------------------------------------------------------

    def _create_vacation_payslip(self, preview=False):
        """Create a single vacation payslip for the approved annual leave.

        Only **one** payslip is created, covering the **current month**
        (the month the leave is approved).  Past months are already
        settled via regular monthly payslip batches; future months will
        be handled by upcoming monthly batches.

        The payslip carries all one-time inputs (VACATION_BAL,
        FLIGHT_TICKET, PENALTY, etc.).

        Called BEFORE _action_validate so x_return_state is still
        'not_applicable', avoiding the vacation-return guard.

        :param preview: when True the payslip is flagged as a provisional
            calculation (``x_is_vacation_preview``) requested by an
            approver mid-chain.  When False (the definitive run) any
            leftover provisional payslip is cancelled first.
        """
        Payslip = self.env['hr.payslip'].sudo()
        today = fields.Date.context_today(self)

        if not preview:
            # The definitive payslip supersedes any provisional run.
            self._cancel_preview_vacation_payslips()

        for leave in self:
            employee = leave.employee_id
            if not employee:
                continue

            if not leave.request_date_from:
                continue

            # Find the employee's salary structure and version
            version = employee.current_version_id
            if not version:
                _logger.warning(
                    'No active version (contract) for employee %s — '
                    'skipping vacation payslip creation.',
                    employee.name,
                )
                continue

            structure = version.struct_id
            if not structure:
                _logger.warning(
                    'No salary structure for employee %s — '
                    'skipping vacation payslip creation.',
                    employee.name,
                )
                continue

            # Use the current month (approval month), not the leave start month.
            # Exception: if no confirmed (done) payslip exists for the PREVIOUS
            # month yet, use the previous month instead.  This avoids the case
            # where a leave approved on the 1st of the month produces an
            # attendance deduction against an empty month (0 records → all days
            # counted absent), while the employee was fully present last month
            # and that month's payslip has not been settled yet.
            month_start = today.replace(day=1)

            # Compute previous-month boundaries
            if month_start.month == 1:
                prev_month_start = date(month_start.year - 1, 12, 1)
            else:
                prev_month_start = date(month_start.year, month_start.month - 1, 1)
            prev_month_end = month_start - timedelta(days=1)

            done_prev = Payslip.search([
                ('employee_id', '=', employee.id),
                ('state', '=', 'done'),
                ('date_from', '>=', prev_month_start),
                ('date_from', '<=', prev_month_end),
            ], limit=1)

            if not done_prev:
                # Previous month not yet settled — use it so that actual
                # attendance data is available and no blank-month deduction fires.
                month_start = prev_month_start
                month_end = prev_month_end
            elif month_start.month == 12:
                month_end = date(month_start.year + 1, 1, 1) - timedelta(days=1)
            else:
                month_end = date(month_start.year, month_start.month + 1, 1) - timedelta(days=1)

            payslip = Payslip.create({
                'employee_id': employee.id,
                'name': '%s — %s — %s/%s' % (
                    'Vacation Payslip (Provisional)' if preview
                    else 'Vacation Payslip',
                    employee.name, month_start.year, month_start.month),
                'date_from': month_start,
                'date_to': month_end,
                'struct_id': structure.id,
                'version_id': version.id,
                'x_leave_id': leave.id,
                'x_is_vacation_preview': preview,
            })

            # Build and attach input lines
            input_vals = self._build_vacation_input_lines(
                leave, employee, payslip)
            if input_vals:
                self.env['hr.payslip.input'].sudo().create(input_vals)

            # Compute the payslip
            payslip.compute_sheet()

            # The definitive run is confirmed as it is generated, so the
            # loans and deductions it presents are actually consumed. A
            # provisional preview stays in draft — it is regenerated on
            # demand and settling installments off a draft calculation
            # would collect money nobody has approved yet.
            if not preview:
                payslip._ksw_auto_confirm_leave_payslip()

            _logger.info(
                '%s payslip #%s created for employee %s '
                '(leave #%s, month %s/%s).',
                'Provisional vacation' if preview else 'Vacation',
                payslip.id, employee.name, leave.id,
                month_start.year, month_start.month,
            )

            leave.sudo().write({'x_vacation_payslip_id': payslip.id})

    @staticmethod
    def _vacation_month_count(leave):
        """Return the number of distinct calendar months the leave spans.

        E.g. Apr 15 – Jun 20 → 3 (April, May, June).
        """
        d_from = leave.request_date_from
        d_to = leave.request_date_to
        if not d_from or not d_to:
            return 1
        months = (d_to.year - d_from.year) * 12 + (d_to.month - d_from.month) + 1
        return max(months, 1)

    def _build_vacation_input_lines(self, leave, employee, payslip):
        """Build the list of hr.payslip.input values for vacation items."""
        vals_list = []
        version_id = payslip.version_id.id
        Input = self.env['hr.payslip.input']

        # 1. Vacation Balance Settlement (FIFO historical wage slicing)
        # For EOS and full-clearance leaves, pin the balance to the leave's
        # request_date_from so recomputing on a later day gives the same figure.
        if leave.x_is_full_clearance:
            vacation_days = self._get_remaining_balance(leave, leave.request_date_from)
            label_prefix = 'Vacation Balance Settlement — Full Clearance'
        elif leave.x_excess_days_accepted and leave.x_annual_portion_days > 0:
            vacation_days = leave.x_annual_portion_days
            label_prefix = 'Vacation Balance Settlement — Annual Portion'
        elif getattr(leave, 'x_is_eos_leave', False):
            # EOS leaves are always 1 calendar day; vacation days to pay out
            # is the full remaining annual leave balance, not the leave duration.
            vacation_days = self._get_remaining_balance(leave, leave.request_date_from)
            label_prefix = 'Vacation Balance Settlement — EOS Payout'
        else:
            vacation_days, _hours = self._annual_cal_days(leave)
            label_prefix = 'Vacation Balance Settlement'

        AnnualLeave = self.env['ksw.annual.leave']
        # If the leave is already validated, its days are included in
        # allocation.leaves_taken.  Pass them as exclude_days so the
        # FIFO calculation doesn't double-count them.
        exclude = leave.number_of_days if leave.state == 'validate' else 0.0
        vac_result = AnnualLeave._compute_historical_vacation_value(
            employee, vacation_days, exclude_days=exclude)
        vacation_balance_value = vac_result['total']

        if vacation_balance_value > 0:
            vals_list.append({
                'payslip_id': payslip.id,
                'version_id': version_id,
                'name': '%s (%s)' % (label_prefix, vac_result['label']),
                'code': 'VACATION_BAL',
                'amount': vacation_balance_value,
            })

        # 2. Flight Ticket Allowance
        if leave.x_flight_ticket_amount:
            vals_list.append({
                'payslip_id': payslip.id,
                'version_id': version_id,
                'name': 'Flight Ticket Allowance',
                'code': 'FLIGHT_TICKET',
                'amount': leave.x_flight_ticket_amount,
            })

        # 3. Penalty Deduction
        if leave.x_penalty_amount:
            vals_list.append({
                'payslip_id': payslip.id,
                'version_id': version_id,
                'name': 'Penalty Deduction',
                'code': 'PENALTY',
                'amount': leave.x_penalty_amount,
            })

        # Note: x_iqama_renewal_amount is recorded on the leave for
        # decision-making only — it is NOT included as a payslip input.

        # 4. Additional Commissions
        if leave.x_additional_commissions:
            vals_list.append({
                'payslip_id': payslip.id,
                'version_id': version_id,
                'name': 'Additional Commissions',
                'code': 'ADDITIONAL_COMMISSIONS',
                'amount': leave.x_additional_commissions,
            })

        # 5. Remaining Loans
        if leave.x_remaining_loans:
            vals_list.append({
                'payslip_id': payslip.id,
                'version_id': version_id,
                'name': 'Remaining Loans',
                'code': 'REMAINING_LOANS',
                'amount': leave.x_remaining_loans,
            })

        # 5b. Other Deductions (sum of the additional-deduction lines).
        # Stored positive on the leave; the OTHER_DEDUCTIONS salary rule
        # negates it so it lands in the DED category.
        other_deductions = getattr(leave, 'x_other_deductions', 0) or 0
        if other_deductions:
            vals_list.append({
                'payslip_id': payslip.id,
                'version_id': version_id,
                'name': 'Other Deductions',
                'code': 'OTHER_DEDUCTIONS',
                'amount': other_deductions,
            })

        # 6. Financial Consideration for Excess Leave (combined leave only)
        fin_consideration = getattr(leave, 'x_financial_consideration_excess', 0) or 0
        if fin_consideration:
            vals_list.append({
                'payslip_id': payslip.id,
                'version_id': version_id,
                'name': 'Financial Consideration for Excess Leave',
                'code': 'FIN_CONSIDERATION',
                'amount': fin_consideration,
            })

        # 7. Visa Cost Recovery for Excess Leave (combined leave only)
        visa_recovery = getattr(leave, 'x_visa_cost_recovery', 0) or 0
        if visa_recovery:
            vals_list.append({
                'payslip_id': payslip.id,
                'version_id': version_id,
                'name': 'Visa Cost Recovery for Excess Leave',
                'code': 'VISA_COST_RECOVERY',
                'amount': visa_recovery,
            })

        # 8. Multi-month HRA advance — PAID vacation months the employee
        #    has NOT already been paid for.
        #
        # The vacation payslip pays HRA up front for every paid
        # vacation month (including the month the vacation payslip
        # itself covers). The regular HRA salary rule is suppressed
        # on vacation payslips (see data/salary_rule_deduction.xml)
        # so the vac month is not double-paid.
        #
        # The advance only holds while the employee is actually away:
        # the monthly batch skips an employee whose return is pending
        # (_get_unresolved_vacation_leaves), so nothing else pays those
        # months.  A request that stalls in the approval chain breaks
        # that assumption — the employee stays at work, draws his
        # ordinary payslips, and the months of the leave are settled in
        # full before the chain ever completes.  Advancing them again
        # pays HRA twice, so every month is checked against what has
        # already been issued (KSWCO leave 4882: requested 29 Jul,
        # approved in Sep, with July and August payslips already done).
        #
        # For combined annual + unpaid leaves (x_excess_days_accepted)
        # and full-balance-clearance leaves, months that fall entirely
        # within the *unpaid* portion do NOT receive HRA.
        #
        # leave.number_of_days already equals the paid-portion duration
        # in every branch of _compute_duration:
        #   - full clearance          → x_clearance_balance
        #   - excess accepted (combo) → x_annual_portion_days
        #   - simple annual           → calendar days of the leave
        paid_months = self._paid_months(leave)
        hra_settled = self._months_already_settled(
            leave, employee, payslip, paid_months, 'hra')
        hra_due = [m for m in paid_months if m not in hra_settled]
        if hra_due:
            version = payslip.version_id or employee.current_version_id
            hra = version.hra or 0.0
            if hra > 0:
                vals_list.append({
                    'payslip_id': payslip.id,
                    'version_id': version_id,
                    'name': 'Advance HRA for %d paid vacation month(s): %s' % (
                        len(hra_due), self._format_months(hra_due)),
                    'code': 'VACATION_HRA',
                    'amount': hra * len(hra_due),
                    # The monthly payslip of any of these months reads
                    # this to subtract its own share (gotcha #124).
                    'x_ksw_advance_months': Input._ksw_format_advance_months(
                        hra_due),
                })

        # 9. Multi-month GOSI advance — ALL vacation months (paid +
        # unpaid).  GOSI is a statutory contribution that the company
        # and employee must pay every month regardless of whether the
        # employee is on paid or unpaid leave, so the unpaid portion
        # of a combined leave still accrues GOSI.
        #
        # Same already-settled filter as the HRA advance above: a month
        # whose ordinary payslip is already out has had its GOSI
        # deducted, and charging it again takes the money twice.
        gosi_months = self._all_vacation_months(leave)
        gosi_settled = self._months_already_settled(
            leave, employee, payslip, gosi_months, 'gosi')
        gosi_due = [m for m in gosi_months if m not in gosi_settled]
        if gosi_due:
            version = payslip.version_id or employee.current_version_id
            wage = version.wage or 0.0
            hra = version.hra or 0.0
            if employee.country_id and employee.country_id.code == 'SA' and (wage + hra) > 0:
                gosi_rate = float(
                    self.env['ir.config_parameter'].sudo().get_param(
                        'ksw_payroll.gosi_rate', '9.75'))
                gosi_per_month = round((wage + hra) * gosi_rate / 100.0)
                if gosi_per_month > 0:
                    vals_list.append({
                        'payslip_id': payslip.id,
                        'version_id': version_id,
                        'name': 'Advance GOSI for %d vacation month(s): %s' % (
                            len(gosi_due), self._format_months(gosi_due)),
                        'code': 'VACATION_GOSI',
                        'amount': gosi_per_month * len(gosi_due),
                        'x_ksw_advance_months': (
                            Input._ksw_format_advance_months(gosi_due)),
                    })

        # Tell the approver why a month is missing from the advance.
        # Guarded on the EOS flag because KSW_eos_leave reuses this
        # builder and drops both advance codes from the result — an EOS
        # payslip skipping them is by design, not a skipped month.
        if (hra_settled or gosi_settled) and not getattr(leave, 'x_is_eos_leave', False):
            self._post_settled_months_note(payslip, hra_settled, gosi_settled)

        return vals_list

    @staticmethod
    def _paid_months(leave):
        """The distinct calendar months spanned by the PAID portion of
        ``leave``, as a sorted list of ``(year, month)`` tuples.

        Unpaid-portion months (from x_excess_days_accepted) contribute
        nothing — those months must not receive HRA on the vacation
        payslip.

        Examples (Apr 15 start):
          * 8-paid-month annual leave (Apr 15 → Nov 30) → 8 months
            (Apr, May, Jun, Jul, Aug, Sep, Oct, Nov).
          * 60-day combined leave, 20 paid days (Apr 15 → May 4) →
            2 months (Apr, May); the 40 unpaid days are ignored.
          * 20-day full-clearance fitting inside Apr → 1 month.
        """
        paid_days = int(round(leave.number_of_days or 0))
        if paid_days <= 0 or not leave.request_date_from:
            return []

        paid_start = leave.request_date_from
        paid_end = paid_start + timedelta(days=paid_days - 1)
        return HrLeave._month_span_list(paid_start, paid_end)

    @staticmethod
    def _paid_months_count(leave):
        """Number of months in :meth:`_paid_months`."""
        return len(HrLeave._paid_months(leave))

    @staticmethod
    def _all_vacation_months(leave):
        """The distinct calendar months spanned by the ENTIRE vacation
        (paid portion + any unpaid excess portion), as a sorted list of
        ``(year, month)`` tuples.

        Used for the GOSI advance, which must cover every month the
        employee is on leave — GOSI is owed by law regardless of
        whether the month is paid or unpaid.

        Examples (Apr 15 start):
          * 60-day combined leave (Apr 15 → Jun 13), 20 paid + 40
            unpaid → 3 months (Apr, May, Jun).
          * 20-day full-clearance (Apr 15 → May 4) → 2 months.
        """
        return HrLeave._month_span_list(
            leave.request_date_from, leave.request_date_to)

    @staticmethod
    def _all_vacation_months_count(leave):
        """Number of months in :meth:`_all_vacation_months`."""
        return len(HrLeave._all_vacation_months(leave))

    @staticmethod
    def _month_span_list(d_from, d_to):
        """The distinct ``(year, month)`` tuples in the inclusive range."""
        if not d_from or not d_to or d_from > d_to:
            return []
        months = []
        cursor = d_from.replace(day=1)
        while cursor <= d_to:
            months.append((cursor.year, cursor.month))
            if cursor.month == 12:
                cursor = cursor.replace(year=cursor.year + 1, month=1)
            else:
                cursor = cursor.replace(month=cursor.month + 1)
        return months

    @staticmethod
    def _month_span(d_from, d_to):
        """Count distinct (year, month) tuples in the inclusive range."""
        return len(HrLeave._month_span_list(d_from, d_to))

    @staticmethod
    def _format_months(months):
        """'Jul 2026, Aug 2026' for a list of ``(year, month)`` tuples."""
        return ', '.join(
            '%s %d' % (calendar.month_abbr[m], y) for y, m in sorted(months))

    def _months_already_settled(self, leave, employee, payslip, months, kind):
        """Which of ``months`` the employee has already been settled for.

        Returns ``{(year, month): payslip reference}`` — the months that
        must be left out of the HRA (``kind='hra'``) or GOSI
        (``kind='gosi'``) advance because an issued payslip already
        carries them.

        The advance exists because an employee who is away draws no
        ordinary payslip for those months.  When the approval chain
        stalls he never leaves, the monthly batch pays him as usual, and
        by the time the chain completes the months are settled: paying
        the advance on top hands him the housing allowance twice and
        takes GOSI off him twice.  So the question is not "how many
        months does the leave span" but "which of them did he actually
        miss".

        Only issued payslips count (``verify`` / ``done``); a draft is
        not money out of the door.  Another leave's vacation payslip
        counts for the months *its* advance covered, which is why the
        span helpers are re-run against that leave.
        """
        months = set(months)
        if not months or not employee:
            return {}

        first, last = min(months), max(months)
        period_start = date(first[0], first[1], 1)
        if last[1] == 12:
            period_end = date(last[0] + 1, 1, 1) - timedelta(days=1)
        else:
            period_end = date(last[0], last[1] + 1, 1) - timedelta(days=1)

        # sudo(): approvers running a provisional calculation have no
        # payroll ACLs of their own.
        slips = self.env['hr.payslip'].sudo().search([
            ('employee_id', '=', employee.id),
            ('state', 'in', ('verify', 'done')),
            ('date_from', '<=', period_end),
            ('date_to', '>=', period_start),
            ('x_is_vacation_preview', '=', False),
        ])

        ordinary_code, advance_code = (
            ('HRA', 'VACATION_HRA') if kind == 'hra'
            else ('GOSI', 'VACATION_GOSI')
        )

        settled = {}
        for slip in slips:
            # This leave's own payslips are the ones being (re)built —
            # they are not a second payment of anything.
            if slip.id == payslip.id or slip.x_leave_id == leave:
                continue
            if slip.x_leave_id:
                covered = (
                    self._paid_months(slip.x_leave_id) if kind == 'hra'
                    else self._all_vacation_months(slip.x_leave_id)
                )
                code = advance_code
            else:
                # An ordinary monthly payslip settles its own month.
                covered = [(slip.date_from.year, slip.date_from.month)]
                code = ordinary_code
            # A zero line means the amount was suppressed (PRIOR_HRA, or
            # a rule condition) — nothing was actually settled.
            if not any(line.code == code and line.total for line in slip.line_ids):
                continue
            ref = slip.number or slip.name
            for month in set(covered) & months:
                settled.setdefault(month, ref)

        if settled:
            _logger.info(
                'Leave #%s (%s): %s advance skips %s — already settled by %s.',
                leave.id, employee.name, ordinary_code,
                self._format_months(settled),
                ', '.join(sorted(set(settled.values()))),
            )
        return settled

    def _post_settled_months_note(self, payslip, hra_settled, gosi_settled):
        """Chatter note naming the months left out of the advance and the
        payslip that already settled each of them.

        Without it a month simply disappears from the figure and the
        approver has no way to tell a correct exclusion from a bug.

        Posted at most once per payslip per distinct set of months: this
        builder is re-run on **every** recompute of a vacation payslip
        (``hr.payslip._refresh_vacation_bal_input`` calls it just to
        re-derive VACATION_BAL), so a plain ``message_post`` would stack
        an identical note on the chatter each time.
        """
        body = Markup(
            '<strong>ℹ️ Advance skipped for already-paid month(s)</strong><br/>'
        )
        for label, settled in (('HRA', hra_settled), ('GOSI', gosi_settled)):
            for month, ref in sorted(settled.items()):
                body += Markup('<b>%(label)s</b> — %(month)s already in %(ref)s<br/>') % {
                    'label': label,
                    'month': self._format_months([month]),
                    'ref': ref,
                }
        # Compare on stripped text: Odoo wraps a posted body in <span>, so
        # the stored value is never byte-identical to what was passed in.
        if any(_plain_text(m.body) == _plain_text(body)
               for m in payslip.sudo().message_ids):
            return
        payslip.sudo().message_post(body=body, subtype_xmlid='mail.mt_note')

    # ------------------------------------------------------------------
    # Finalised requests (KSW_annual_leave._check_final_reversal_rights)
    # ------------------------------------------------------------------

    def _has_confirmed_payslip(self):
        """True when this leave's vacation payslip is confirmed.

        sudo(): ``x_vacation_payslip_ids`` carries a model-level ``groups=``,
        and this runs for every caller.
        """
        if super()._has_confirmed_payslip():
            return True
        return any(
            slip.state == 'done'
            for slip in self.sudo().x_vacation_payslip_ids
        )

    def _is_finalised(self):
        """A leave whose vacation payslip is confirmed is finalised.

        The money is out of the door at that point, so undoing the leave
        would cancel a paid payslip — exactly the class of damage that cost
        KSWCO SLIP/11307 (a cancelled *paid* slip re-collects its deductions
        the following month).
        """
        return super()._is_finalised() or self._has_confirmed_payslip()

    # ------------------------------------------------------------------
    # Cancel vacation payslip on refuse / reset-to-draft
    # ------------------------------------------------------------------

    def _cancel_vacation_payslips(self):
        """Cancel any vacation payslips linked to these leaves.

        sudo(): this runs as a side effect of refuse / back-to-approval /
        reset-to-draft, all of which a direct manager or KSW Supervisor is
        entitled to do. Those users have no payroll ACLs, and both
        ``x_vacation_payslip_ids`` and ``x_vacation_payslip_id`` carry a
        model-level ``groups=``, so reading them as the calling user raises
        AccessError and rolls the whole refuse back. Authorisation for the
        action itself is enforced by the callers.
        """
        records = self.sudo()
        for leave in records:
            payslips = leave.x_vacation_payslip_ids.filtered(
                lambda p: p.state != 'cancel'
            )
            if not payslips:
                continue
            # A confirmed slip going to 'cancel' releases the installments it
            # settled: hr.payslip.write in KSW_deduction sees done -> cancel
            # and calls _sync_deductions_on_reset, which reverts the paid
            # lines to pending and merges any forwarded remainder back. The
            # money is un-collected, so say so in the chatter rather than
            # leaving it to be discovered on next month's payslip.
            settled = payslips.filtered(lambda p: p.state == 'done')
            released = leave._released_installment_total(settled)
            payslips.write({'state': 'cancel'})
            if settled:
                leave._post_installment_release_note(settled, released)
        records.filtered('x_vacation_payslip_id').write({
            'x_vacation_payslip_id': False,
        })

    @staticmethod
    def _released_installment_total(payslips):
        """Total the cancelled payslips had actually collected.

        Guarded on the field name: ``x_ksw_ded_collected`` is declared in
        KSW_deduction, which **depends on** this module, so it is absent
        whenever KSW_payroll runs without it.
        """
        if not payslips or 'x_ksw_ded_collected' not in payslips._fields:
            return 0.0
        return sum(payslips.mapped('x_ksw_ded_collected'))

    def _post_installment_release_note(self, payslips, released):
        """Record on the leave that a settlement was undone.

        ``sudo()`` for the message itself (gotcha #11): this runs off refuse
        / return / reset, which a direct manager may trigger, and
        ``mail.message`` create is gated on access to the document.
        """
        self.ensure_one()
        names = ', '.join(
            p.number or p.name or str(p.id) for p in payslips)
        body = Markup(
            '<strong>&#8630; Settlement payslip cancelled</strong><br/>'
            '<b>Payslip:</b> %(slips)s<br/>'
        ) % {'slips': names}
        if released:
            body += Markup(
                '<b>Deductions released back to pending:</b> '
                '%(amt).2f SAR<br/>'
                'These installments were <b>not</b> collected after all and '
                'will be taken by a later payroll run.'
            ) % {'amt': released}
        else:
            body += Markup(
                'No deduction installments had been collected by it.')
        self.sudo().message_post(body=body, subtype_xmlid='mail.mt_note')

    def _release_payslips_for_reversal(self, what):
        """Cancel an auto-confirmed vacation settlement so a reversal can run.

        The definitive vacation payslip is confirmed the moment the chain
        completes (``_ksw_auto_confirm_leave_payslip``) so its loans and
        deductions are consumed. That makes ``_has_confirmed_payslip()``
        true for every approved leave, which used to be an outright block on
        the GM's return-to-approver wizard — so the block now applies only to
        a payslip a **payroll officer** confirmed, which is a person
        asserting the money was paid.
        """
        super()._release_payslips_for_reversal(what)
        records = self.sudo()
        # Only a **confirmed** payslip is released. A draft provisional
        # calculation has settled nothing and is deliberately left alone —
        # the definitive run supersedes it via
        # `_cancel_preview_vacation_payslips`, and a mid-chain return that
        # threw the approvers' figures away would be its own bug.
        confirmed = {
            leave.id: leave.x_vacation_payslip_ids.filtered(
                lambda p: p.state == 'done')
            for leave in records
        }
        # Fail fast, before anything is written.
        for leave in records:
            manual = confirmed[leave.id].filtered(
                lambda p: not p.x_vacation_auto_confirmed)
            if manual:
                raise UserError(_(
                    'This request has a payslip a payroll officer confirmed '
                    'by hand (%(slips)s), so it is treated as paid. Handle '
                    'the payslip first — cancelling a paid payslip releases '
                    'its deductions and re-collects them on the next run. '
                    'The request cannot be %(what)s until then.',
                    slips=', '.join(
                        p.number or p.name or str(p.id) for p in manual),
                    what=what,
                ))
        for leave in records:
            slips = confirmed[leave.id]
            if not slips:
                continue
            released = leave._released_installment_total(slips)
            slips.write({'state': 'cancel'})
            leave._post_installment_release_note(slips, released)
            if leave.x_vacation_payslip_id in slips:
                leave.write({'x_vacation_payslip_id': False})

    # ------------------------------------------------------------------
    # Attendance-sheet "needs attention" flags
    # ------------------------------------------------------------------

    def _refresh_sheet_blocked_flags(self):
        """Re-derive the blocked flags of any attendance sheet this leave
        affects.

        A leave decision changes whether a sheet can be sent to payroll
        without writing anything on the sheet, so nothing else refreshes
        it. The flags are display-only (the confirm guard always
        re-evaluates live), but a stale "Needs Attention" badge is exactly
        the sort of thing that trains people to ignore the badge.
        """
        if 'ksw.attendance.sheet' not in self.env:
            return
        self.env['ksw.attendance.sheet']._refresh_blocked_for_leaves(self)

    def action_confirm_return_manager(self):
        result = super().action_confirm_return_manager()
        self._refresh_sheet_blocked_flags()
        return result

    def _action_validate(self, check_state=True):
        result = super()._action_validate(check_state=check_state)
        self._refresh_sheet_blocked_flags()
        return result

    def action_refuse(self):
        annual_multi = self.filtered(
            lambda l: l.holiday_status_id
            and l.holiday_status_id.leave_validation_type == 'annual_multi'
        )
        result = super().action_refuse()
        if annual_multi:
            annual_multi._cancel_vacation_payslips()
        self._refresh_sheet_blocked_flags()
        return result

    def _move_validate_leave_to_confirm(self):
        """Cancel vacation payslips when using 'Back to Approval'."""
        annual_multi = self.filtered(
            lambda l: l.holiday_status_id
            and l.holiday_status_id.leave_validation_type == 'annual_multi'
        )
        result = super()._move_validate_leave_to_confirm()
        if annual_multi:
            annual_multi._cancel_vacation_payslips()
        return result

    def action_draft(self):
        annual_multi = self.filtered(
            lambda l: l.holiday_status_id
            and l.holiday_status_id.leave_validation_type == 'annual_multi'
        )
        result = super().action_draft()
        if annual_multi:
            annual_multi._cancel_vacation_payslips()
        return result

    def unlink(self):
        """Cancel any live vacation payslip before the leave disappears.

        ``hr.payslip.x_leave_id`` is a plain many2one, so deleting the leave
        only NULLs it — the payslip (usually the provisional one produced by
        "Calculate Vacation") would survive as an orphan draft nobody can
        trace back. A payslip a payroll officer confirmed by hand is left
        alone: that is already paid and must stay on the books. One the
        approval chain auto-confirmed is cancelled like any other, which
        releases the installments it settled.
        """
        payslips = self.sudo().x_vacation_payslip_ids.filtered(
            lambda p: p.state != 'cancel'
            and (p.state != 'done' or p.x_vacation_auto_confirmed)
        )
        if payslips:
            payslips.write({'state': 'cancel'})
        return super().unlink()

    def action_print_vacation_report(self):
        """Return the Annual Vacation Report PDF action for this leave."""
        self.ensure_one()
        return self.env.ref(
            'KSW_payroll.action_report_annual_vacation'
        ).report_action(self)

    def action_open_vacation_payslips(self):
        """Open the vacation payslip(s) linked to this leave."""
        self.ensure_one()
        payslip_ids = self.x_vacation_payslip_ids.ids
        if len(payslip_ids) == 1:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'hr.payslip',
                'view_mode': 'form',
                'res_id': payslip_ids[0],
                'target': 'current',
            }
        return {
            'type': 'ir.actions.act_window',
            'name': 'Vacation Payslips',
            'res_model': 'hr.payslip',
            'view_mode': 'list,form',
            'domain': [('id', 'in', payslip_ids)],
            'target': 'current',
        }

    def action_recompute_vacation_payslip(self):
        """Cancel the existing vacation payslip and recreate it from current leave inputs.

        Use this when the leave's financial inputs (penalty, commissions, loans,
        flight ticket, etc.) were updated after the payslip was already generated.
        The old payslip is cancelled (kept for audit) and a fresh one is created.
        """
        self.ensure_one()
        if not self.env.su and not (
            self.env.user.has_group('om_hr_payroll.group_hr_payroll_user')
            or self.env.user.has_group('KSW_annual_leave.group_annual_leave_hr')
        ):
            raise UserError('Only HR Payroll users or HR Approvers can recompute vacation payslips.')
        if not self.x_vacation_payslip_id:
            raise UserError('No vacation payslip found on this leave to recompute.')
        # A payslip generated mid-chain stays provisional when recomputed —
        # it only becomes definitive at GM final approval.
        preview = self.sudo().x_vacation_payslip_id.x_is_vacation_preview
        # sudo(): authorisation was checked above; annual-leave HR users
        # lack payroll ACLs and the group-restricted x_vacation_payslip_ids
        # field that these helpers read/write.
        self.sudo()._cancel_vacation_payslips()
        self.sudo()._create_vacation_payslip(preview=preview)
        self.sudo().message_post(
            body=Markup(
                '<strong>🔄 Vacation Payslip Recomputed</strong><br/>'
                '<b>By:</b> %(user)s'
            ) % {'user': self.env.user.name},
            subtype_xmlid='mail.mt_note',
        )

