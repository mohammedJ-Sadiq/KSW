from dateutil.relativedelta import relativedelta

from odoo import api, fields, models, _

from .hr_payslip import _KSW_LINE_ORDER


class HrLeave(models.Model):
    """Full deduction picture on the leave's Accounting page.

    A vacation / EOS approval is the moment accounting decides what the
    employee walks away with, so the reviewer needs the whole obligation in
    front of them — not just the slice this payslip happens to afford.
    These fields are read-only insight; nothing here changes what is
    collected.  The collection itself happens on the payslip
    (`hr.payslip._ksw_apply_deduction_priority`), which presents the full
    amount on a vacation / EOS run and settles only the affordable part.

    No model-level ``groups=`` on any of them: they are referenced in
    ``invisible=`` on elements the ACC / GM approvers see, and those users
    are not necessarily ``hr.group_hr_user`` — a model gate would drop the
    fields from ``fields_get()`` and crash the form (pitfall #31).  Every
    compute reads the restricted source data through ``sudo()`` and
    visibility is view-level only.
    """
    _inherit = 'hr.leave'

    x_ksw_ded_outstanding = fields.Float(
        string='Outstanding Loans & Deductions', digits=(16, 2),
        compute='_compute_ksw_leave_deduction_picture',
        help='Everything the employee still owes: all pending installments '
             'across every active deduction, whatever month they fall in.',
    )
    x_ksw_ded_month_total = fields.Float(
        string='Due at This Payroll Run', digits=(16, 2),
        compute='_compute_ksw_leave_deduction_picture',
        help='Pending installments payroll will try to collect now: those '
             'scheduled for the current month plus anything still pending '
             'from an earlier month.',
    )
    x_ksw_ded_has_calc = fields.Boolean(
        compute='_compute_ksw_leave_deduction_picture',
    )
    x_ksw_ded_presented = fields.Float(
        string='Presented on the Payslip', digits=(16, 2),
        compute='_compute_ksw_leave_deduction_picture',
        help='Total deduction installments carried on the vacation / EOS '
             'payslip. The full amount is shown even when it drives the '
             'net negative.',
    )
    x_ksw_ded_collected = fields.Float(
        string='Collected by the Payslip', digits=(16, 2),
        compute='_compute_ksw_leave_deduction_picture',
        help='The part the pay could actually absorb. Only this part is '
             'marked paid on the deduction schedule.',
    )
    x_ksw_ded_carried = fields.Float(
        string='Not Collected (Still Pending)', digits=(16, 2),
        compute='_compute_ksw_leave_deduction_picture',
        help='The part the pay could not cover. It is NOT marked paid — it '
             'stays pending and is collected by a later payroll run.',
    )
    x_ksw_ded_shortfall = fields.Boolean(
        compute='_compute_ksw_leave_deduction_picture',
        help='True when the payslip could not cover every deduction '
             'presented on it.',
    )
    x_ksw_ded_manual_total = fields.Float(
        string='Entered by Hand', digits=(16, 2),
        compute='_compute_ksw_leave_deduction_picture',
        help='Remaining Loans plus Additional Deductions as typed on this '
             'form.',
    )
    x_ksw_ded_manual_duplicate = fields.Boolean(
        compute='_compute_ksw_leave_deduction_picture',
        help='True when amounts were typed by hand while the employee also '
             'has outstanding installments — which payroll now collects '
             'automatically, so the two may be charging the same money '
             'twice.',
    )

    @api.depends('employee_id', 'x_vacation_payslip_ids',
                 'x_vacation_payslip_ids.state',
                 'x_vacation_payslip_ids.input_line_ids.amount',
                 'x_vacation_payslip_ids.input_line_ids.x_ksw_uncollected',
                 'x_remaining_loans', 'x_other_deductions')
    def _compute_ksw_leave_deduction_picture(self):
        for leave in self:
            # sudo(): the employee totals are gated behind hr.group_hr_user
            # and the ACC / GM approvers reading this panel are not HR.
            employee = leave.employee_id.sudo()
            leave.x_ksw_ded_outstanding = (
                employee.x_deduction_outstanding_total if employee else 0.0)
            leave.x_ksw_ded_month_total = (
                employee.x_deduction_monthly_total if employee else 0.0)

            # Payroll now pulls every outstanding installment onto the
            # settlement, so a hand-typed loan balance next to it charges the
            # same money twice. Flag it rather than silently netting an
            # accountant's entry away — deciding which one to keep is theirs.
            manual = ((leave.x_remaining_loans or 0.0)
                      + (leave.x_other_deductions or 0.0))
            leave.x_ksw_ded_manual_total = manual
            leave.x_ksw_ded_manual_duplicate = bool(
                manual and leave.x_ksw_ded_outstanding)

            payslip = leave._current_vacation_payslip()
            if not payslip:
                leave.x_ksw_ded_has_calc = False
                leave.x_ksw_ded_presented = 0.0
                leave.x_ksw_ded_collected = 0.0
                leave.x_ksw_ded_carried = 0.0
                leave.x_ksw_ded_shortfall = False
                continue
            payslip = payslip.sudo()
            leave.x_ksw_ded_has_calc = True
            leave.x_ksw_ded_presented = payslip.x_ksw_ded_presented
            leave.x_ksw_ded_collected = payslip.x_ksw_ded_collected
            leave.x_ksw_ded_carried = payslip.x_ksw_ded_carried
            leave.x_ksw_ded_shortfall = bool(payslip.x_ksw_ded_carried)

    # ------------------------------------------------------------------
    # Per-installment detail for the Annual Vacation Report
    # ------------------------------------------------------------------

    def _ksw_report_deduction_rows(self):
        """``(rows, from_payslip)`` — one row per installment.

        Once the vacation / EOS payslip exists, its ``KSW_DED_<line id>``
        inputs are the record: what it presented, and what it collected
        (``amount - x_ksw_uncollected``, the same split
        `hr.payslip._sync_deductions_on_done` settles). Before that, the
        pending installments the payslip will carry — every one, not just
        this month's (`_ksw_pending_lines_domain(full=True)`), so the rows add
        up to "Outstanding Loans & Deductions".
        """
        self.ensure_one()
        Line = self.env['ksw.deduction.line'].sudo()
        payslip = self._current_vacation_payslip()
        if payslip:
            rows = []
            for inp in payslip.sudo().input_line_ids.sorted('sequence'):
                if not (inp.code and inp.code.startswith('KSW_DED_')
                        and inp.code[8:].isdigit()):
                    continue
                line = Line.browse(int(inp.code[8:])).exists()
                uncollected = inp.x_ksw_uncollected or 0.0
                rows.append(self._ksw_report_deduction_row(
                    line, inp.amount, inp.amount - uncollected, uncollected,
                    fallback=inp.name))
            return rows, True
        if not self.employee_id:
            return [], False
        lines = Line.search(
            self.env['hr.payslip']._ksw_pending_lines_domain(
                [self.employee_id.id], False, full=True),
            order=_KSW_LINE_ORDER)
        return [self._ksw_report_deduction_row(l, l.amount) for l in lines], False

    @api.model
    def _ksw_report_deduction_row(self, line, amount, collected=0.0,
                                  uncollected=0.0, fallback=''):
        ded = line.deduction_id
        return {
            'type': ded.type_id.name or fallback,
            'ref': ded.name or '',
            'installment': ('%d/%d' % (line.sequence, ded.installments)
                            if ded else ''),
            'period': line.period_date,
            'amount': amount,
            'collected': collected,
            'uncollected': uncollected,
        }

    # ------------------------------------------------------------------
    # Drill-downs — same domains as the totals above, so the list always
    # adds up to the figure on the form.  Record rules still apply: an
    # approver may see a line whose parent deduction they cannot open
    # (see ksw.deduction.line.action_open_deduction).
    # ------------------------------------------------------------------

    def _ksw_leave_installments_action(self, name, extra_domain=None):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': name,
            'res_model': 'ksw.deduction.line',
            'view_mode': 'list,form',
            'views': [
                (self.env.ref(
                    'KSW_deduction.ksw_deduction_line_view_list').id, 'list'),
                (self.env.ref(
                    'KSW_deduction.ksw_deduction_line_view_form').id, 'form'),
            ],
            'search_view_id': self.env.ref(
                'KSW_deduction.ksw_deduction_line_view_search').id,
            'domain': [
                ('employee_id', '=', self.employee_id.id),
                ('state', '=', 'pending'),
                ('deduction_id.state', '=', 'active'),
            ] + (extra_domain or []),
            'context': {'search_default_group_deduction': 1, 'create': False},
            'target': 'current',
        }

    def action_view_leave_outstanding_installments(self):
        """Drill-down behind "Outstanding Loans & Deductions"."""
        return self._ksw_leave_installments_action(
            _('Outstanding Deductions — %s') % (self.employee_id.name or ''))

    def action_view_leave_month_installments(self):
        """Drill-down behind "Due at This Payroll Run".

        No lower period bound — an installment left pending in an earlier
        month is collected by the next run, mirroring
        `hr.payslip._ksw_pending_lines_domain`.
        """
        self.ensure_one()
        period_end = (fields.Date.context_today(self).replace(day=1)
                      + relativedelta(months=1, days=-1))
        return self._ksw_leave_installments_action(
            _('Deductions Due Now — %s') % (self.employee_id.name or ''),
            [('period_date', '<=', period_end)])
