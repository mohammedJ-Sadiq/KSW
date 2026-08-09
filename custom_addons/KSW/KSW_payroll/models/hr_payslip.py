import logging
from calendar import monthrange
from datetime import datetime, time, timedelta

from markupsafe import Markup

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

# Group allowed to reverse a payslip (reject / reset to draft / refund).
PAYROLL_MANAGER_GROUP = 'om_hr_payroll.group_hr_payroll_manager'
# Group allowed to issue a revision (additive — pays a shortfall).
PAYROLL_OFFICER_GROUP = 'om_hr_payroll.group_hr_payroll_user'


def _days_in_month(d):
    """Number of calendar days in the month of ``d`` (e.g. 31, 30, 28/29).

    Used as the daily-wage / deduction divisor so per-day amounts reflect
    the actual month length instead of a fixed 30.  ``d`` may be a date or
    a datetime; falls back to 30 if ``d`` is falsy.
    """
    if not d:
        return 30.0
    return float(monthrange(d.year, d.month)[1])


# ======================================================================
# hr.payroll.structure — absence-only deduction flag
# ======================================================================

class HrPayrollStructure(models.Model):
    _inherit = 'hr.payroll.structure'

    x_absence_only_deduction = fields.Boolean(
        string='Absence-Only Deduction',
        default=False,
        help='When enabled, the payslip deducts only for fully absent or '
             'unpresented days.  Late arrivals and early departures are '
             'tracked in attendance but carry no salary penalty.',
    )

DAILY_HOURS = 8.0
# Fallback only.  The live daily-wage / deduction divisor is the actual
# number of days in the payslip's month — see _days_in_month().
DAYS_PER_MONTH = 30.0


# ======================================================================
# Extend worked-day line — display fields + deduction amount
# ======================================================================

# Maps worked-day code → (count_unit, value_unit)
_WD_UNITS = {
    'WORK100':   ('days',  'hours'),
    'ATT_ABS':   ('days',  'hours'),
    'ATT_LATE':  ('times', 'hours'),
    'ATT_EARLY': ('times', 'hours'),
    'ATT_DED':   ('',      ''),
}


class HrPayslipWorkedDays(models.Model):
    _inherit = 'hr.payslip.worked_days'

    number_of_days = fields.Float(digits=(16, 2))
    number_of_hours = fields.Float(digits=(16, 2))
    amount = fields.Float(
        string='Amount',
        digits=(16, 0),
        help='Monetary amount associated with this worked-day entry '
             '(e.g. total attendance deduction).',
    )

    x_count_display = fields.Char(
        string='Count', compute='_compute_display_fields',
    )
    x_value_display = fields.Char(
        string='Value', compute='_compute_display_fields',
    )

    @api.depends('code', 'number_of_days', 'number_of_hours')
    def _compute_display_fields(self):
        for rec in self:
            c_unit, v_unit = _WD_UNITS.get(rec.code or '', ('', ''))
            # Count
            days = rec.number_of_days or 0.0
            if not c_unit:
                rec.x_count_display = ''
            else:
                rec.x_count_display = '%.2f (%s)' % (days, c_unit)
            # Value
            hrs = rec.number_of_hours or 0.0
            if not v_unit:
                rec.x_value_display = ''
            else:
                rec.x_value_display = '%.2f (%s)' % (hrs, v_unit)


# ======================================================================
# Payslip lines — integer display
# ======================================================================

class HrPayslipLine(models.Model):
    _inherit = 'hr.payslip.line'

    amount = fields.Float(digits=(16, 0))
    quantity = fields.Float(digits=(16, 0))
    total = fields.Float(digits=(16, 0))


# ======================================================================
# Payslip: worked-day population + vacation-return guard
# ======================================================================

class HrPayslip(models.Model):
    _inherit = 'hr.payslip'

    # ------------------------------------------------------------------
    # Reverse link to the leave that generated this vacation payslip
    # ------------------------------------------------------------------

    x_leave_id = fields.Many2one(
        'hr.leave', string='Related Leave', readonly=True, copy=False,
        help='The annual leave that triggered the creation of this '
             'vacation payslip.  Set automatically by '
             '_create_vacation_payslip().',
    )

    x_is_vacation_preview = fields.Boolean(
        string='Provisional Calculation', readonly=True, copy=False,
        help='This payslip is a provisional vacation calculation produced '
             'before the leave finished its approval chain.  Figures may '
             'still change; it is cancelled and replaced by the definitive '
             'payslip when the GM gives final approval.',
    )

    # ------------------------------------------------------------------
    # Revision payslip — re-issue a period that was already paid
    # ------------------------------------------------------------------
    # None of these carry a model-level ``groups=``: ``x_is_revision`` is
    # referenced in ``invisible=`` on the header button, which would trip
    # the OWL "field is undefined" crash for any user who can see the
    # button but not the field (pitfall #31).  Visibility is view-level.

    x_is_revision = fields.Boolean(
        string='Revision', readonly=True, copy=False,
        help='This payslip re-issues a period that was already confirmed '
             'and paid.  It recomputes the whole period with current data '
             'and subtracts everything already paid, so its NET is the '
             'difference still owed to the employee.',
    )

    x_revised_payslip_id = fields.Many2one(
        'hr.payslip', string='Revision Of', readonly=True, copy=False,
        help='The confirmed payslip this revision was issued from.',
    )

    x_revision_ids = fields.One2many(
        'hr.payslip', 'x_revised_payslip_id', string='Revisions',
        readonly=True, copy=False,
    )

    x_revision_count = fields.Integer(
        string='Revision Count', compute='_compute_revision_count',
    )

    x_prior_net_paid = fields.Float(
        string='Already Paid', readonly=True, copy=False, digits=(16, 0),
        help='Total NET already paid to the employee for this period by '
             'the payslip(s) this revision supersedes.',
    )

    x_deserved_net = fields.Float(
        string='Deserved Net', compute='_compute_deserved_net',
        digits=(16, 0),
        help='What the employee should have received for this period in '
             'total — the difference payable plus what was already paid.',
    )

    @api.depends('x_revision_ids')
    def _compute_revision_count(self):
        for slip in self:
            slip.x_revision_count = len(slip.x_revision_ids)

    @api.depends('x_net_wage', 'x_prior_net_paid')
    def _compute_deserved_net(self):
        for slip in self:
            slip.x_deserved_net = slip.x_net_wage + slip.x_prior_net_paid

    # ------------------------------------------------------------------
    # Daily / Hourly wage (read-only info fields)
    # ------------------------------------------------------------------

    x_daily_wage = fields.Float(
        string='Daily Wage',
        compute='_compute_wage_rates',
        digits=(16, 2),
        help='(Wage + DA + Travel + Meal + Medical + Other) divided by the '
             'number of days in the payslip month. '
             'Excludes housing allowance (HRA).',
    )
    x_hourly_wage = fields.Float(
        string='Hourly Wage',
        compute='_compute_wage_rates',
        digits=(16, 2),
        help='Daily wage / 8 hours.',
    )

    # ------------------------------------------------------------------
    # Net wage — stored so it can be shown in the batch payslip list
    # ------------------------------------------------------------------

    x_net_wage = fields.Float(
        string='Net Salary',
        compute='_compute_net_wage',
        store=True,
        digits=(16, 0),
        help='Total of the NET salary rule line after compute_sheet().',
    )

    @api.depends('line_ids.total', 'line_ids.code')
    def _compute_net_wage(self):
        for slip in self:
            net_lines = slip.line_ids.filtered(lambda l: l.code == 'NET')
            slip.x_net_wage = sum(net_lines.mapped('total'))

    @api.depends('date_from', 'version_id.wage',
                 'version_id.travel_allowance', 'version_id.mobile_allowance',
                 'version_id.other_allowance')
    def _compute_wage_rates(self):
        for slip in self:
            v = slip.version_id.sudo()
            base = (
                (v.wage or 0.0)
                + (v.travel_allowance or 0.0)
                + (v.mobile_allowance or 0.0)
                + (v.other_allowance or 0.0)
            )
            daily = base / _days_in_month(slip.date_from) if base else 0.0
            slip.x_daily_wage = daily
            slip.x_hourly_wage = daily / DAILY_HOURS if daily else 0.0

    # ------------------------------------------------------------------
    # compute_sheet override
    # ------------------------------------------------------------------

    def compute_sheet(self):
        """Guard against unresolved vacations, ensure worked-day lines
        are populated, and inject prior-payslip adjustment inputs so
        fixed monthly amounts (HRA) are not paid twice.

        When a prior vacation payslip exists in the same period and the
        employee's return has been HR-confirmed, we re-generate worked-day
        lines starting from the return date so that pre-vacation attendance
        is NOT double-counted.
        """
        for payslip in self:
            self._check_unresolved_vacation(payslip)

            # Refresh VACATION_BAL on vacation payslips so that contract
            # date / wage changes are reflected when recomputing.
            self._refresh_vacation_bal_input(payslip)

            # If a vacation return exists, force re-generation of worked
            # days from the return date (not the payslip start).
            absence_only = bool(
                payslip.struct_id
                and payslip.struct_id.x_absence_only_deduction
            )
            return_date = self._get_vacation_return_date(payslip)

            # For vacation / EOS payslips (x_leave_id set), cap the
            # attendance window to the day BEFORE the leave starts.
            # Days from the leave start to payslip.date_to are injected
            # as absent so ATTDED deducts for them; the VACATION_BAL /
            # EOS input lines compensate for those vacation-period days.
            effective_to = None
            if payslip.x_leave_id:
                leave_start = payslip.x_leave_id.request_date_from
                if leave_start and leave_start <= payslip.date_to:
                    effective_to = leave_start - timedelta(days=1)

            # Clear and regenerate when:
            #  - there is a vacation return (use return date as new start), OR
            #  - the structure is absence-only, OR
            #  - this is a vacation/EOS payslip (attendance window must be
            #    capped to the day before leave starts), OR
            #  - this is a revision — re-reading live attendance is the
            #    entire point of re-issuing the period.
            if (return_date or absence_only or effective_to is not None
                    or payslip.x_is_revision) \
                    and payslip.worked_days_line_ids:
                payslip.worked_days_line_ids.unlink()

            if not payslip.worked_days_line_ids:
                self._ensure_worked_days(payslip, effective_from=return_date,
                                         effective_to=effective_to,
                                         absence_only=absence_only)

            # A revision recomputes the whole period as if it were the
            # single payslip of that month, and then subtracts the total
            # already paid via PRIOR_NET.  PRIOR_HRA / PRIOR_GOSI would
            # subtract the same HRA and GOSI a second time.
            if not payslip.x_is_revision:
                self._inject_prior_hra_input(payslip)
        res = super().compute_sheet()
        # Re-derive NET from the already-rounded (digits=(16,0)) GROSS and DED
        # line amounts.  The base engine accumulates categories using
        # currency.round (SAR = 2 dp), but our amount field stores integers.
        # When an input is fractional (e.g. 87.5 SAR loan), the engine sums
        # -87.5 into categories.DED and the NET lands on 6153.5 → rounds to
        # 6154, while the displayed KSW_DEDUCTIONS line shows -88 (87.5
        # rounded to integer).  Recomputing NET from the displayed amounts
        # ensures GROSS − Σ(deductions) = NET with no 1-SAR display gap.
        for payslip in self:
            gross_lines = payslip.line_ids.filtered(lambda l: l.code == 'GROSS')
            net_lines = payslip.line_ids.filtered(lambda l: l.code == 'NET')
            if not gross_lines or not net_lines:
                continue
            gross_amt = gross_lines[0].amount
            ded_amt = sum(
                l.amount for l in payslip.line_ids
                if l.category_id.code == 'DED'
            )
            correct_net = gross_amt + ded_amt
            if net_lines[0].amount != correct_net:
                net_lines[0].amount = correct_net
            # Update SICK_PAY_ADJ line name to show bracket details.
            sick_lines = payslip.line_ids.filtered(lambda l: l.code == 'SICK_PAY_ADJ')
            if sick_lines:
                sick_lines[0].name = self._sick_pay_adj_line_name(payslip)
        return res

    def _sick_pay_adj_line_name(self, payslip):
        """Build a descriptive name for the SICK_PAY_ADJ payslip line.

        Example: 'Sick Leave Pay Adjustment (18.9d at 75% + 5.0d unpaid)'
        """
        sick_type = self.env.ref('KSW_leave_types.leave_type_sick', raise_if_not_found=False)
        if not sick_type:
            return 'Sick Leave Pay Adjustment'
        alloc = self.env['hr.leave.allocation'].sudo().search([
            ('employee_id', '=', payslip.employee_id.id),
            ('holiday_status_id', '=', sick_type.id),
            ('state', '=', 'validate'),
            ('date_from', '<=', payslip.date_to),
        ], order='date_from desc', limit=1)
        if not alloc:
            return 'Sick Leave Pay Adjustment'
        p = self.env['ir.config_parameter'].sudo()
        t_full = int(p.get_param('ksw_payroll.sick_full_pay_days', '30'))
        t_partial = int(p.get_param('ksw_payroll.sick_partial_pay_days', '90'))
        pct = float(p.get_param('ksw_payroll.sick_partial_pay_pct', '75'))
        year_start = alloc.date_from
        year_end = alloc.date_to or payslip.date_to
        ps_start = payslip.date_from
        ps_end = payslip.date_to
        sick_leaves = self.env['hr.leave'].sudo().search([
            ('employee_id', '=', payslip.employee_id.id),
            ('holiday_status_id', '=', sick_type.id),
            ('state', '=', 'validate'),
            ('request_date_from', '<=', year_end),
            ('request_date_to', '>=', year_start),
        ])
        prior = period = 0.0
        for lv in sick_leaves:
            ls = max(lv.request_date_from, year_start)
            le = min(lv.request_date_to, year_end)
            # Sick leave counts all calendar days — use them directly.
            if le < ps_start:
                prior += (le - ls).days + 1
            elif ls > ps_end:
                pass
            elif ls >= ps_start and le <= ps_end:
                period += (le - ls).days + 1
            else:
                pre_cal = max(0, (ps_start - ls).days)
                in_cal = (min(le, ps_end) - max(ls, ps_start)).days + 1
                prior += pre_cal
                period += in_cal
        if period <= 0:
            return 'Sick Leave Pay Adjustment'
        end_cum = prior + period
        d_partial = max(0.0, min(end_cum, t_partial) - max(prior, t_full))
        d_unpaid = max(0.0, end_cum - max(prior, t_partial))
        pct_int = int(pct)
        parts = []
        if d_partial > 0:
            parts.append(f'{d_partial:.1f}d at {pct_int}% pay')
        if d_unpaid > 0:
            parts.append(f'{d_unpaid:.1f}d unpaid')
        if not parts:
            return 'Sick Leave Pay Adjustment'
        return f'Sick Leave Pay Adjustment ({", ".join(parts)})'

    # ------------------------------------------------------------------
    # Refresh VACATION_BAL input on vacation payslips
    # ------------------------------------------------------------------

    def _refresh_vacation_bal_input(self, payslip):
        """Recompute the VACATION_BAL input line on a vacation payslip.

        This ensures that contract-date or wage changes on hr.version
        are reflected when the payslip is recomputed.  Only applies to
        vacation payslips (those linked to an annual leave via x_leave_id).
        """
        leave = payslip.x_leave_id
        if not leave:
            return

        employee = payslip.employee_id
        if not employee:
            return

        # Delegate to hr.leave._build_vacation_input_lines which knows
        # how to determine vacation_days from the leave type/flags.
        HrLeave = self.env['hr.leave'].sudo()
        new_vals = HrLeave._build_vacation_input_lines(leave, employee, payslip)

        # Extract the new VACATION_BAL entry
        new_vac = next((v for v in new_vals if v.get('code') == 'VACATION_BAL'), None)

        # Find the existing VACATION_BAL input line
        existing_vac = payslip.input_line_ids.filtered(lambda i: i.code == 'VACATION_BAL')

        if new_vac and existing_vac:
            existing_vac.sudo().write({
                'name': new_vac['name'],
                'amount': new_vac['amount'],
            })
        elif new_vac and not existing_vac:
            self.env['hr.payslip.input'].sudo().create(new_vac)
        elif not new_vac and existing_vac:
            existing_vac.sudo().unlink()

    # ------------------------------------------------------------------
    # Prevent double-payment of HRA across multiple payslips in a month
    # ------------------------------------------------------------------

    def _get_vacation_return_date(self, payslip):
        """Find the latest HR-confirmed annual-leave return date for the
        payslip's employee within the payslip period.

        Returns the return date (``date``) or ``None``.
        Leaves whose vacation payslip IS the current payslip are excluded
        (so the vacation payslip itself is never affected).
        """
        if not payslip.employee_id or not payslip.date_from or not payslip.date_to:
            return None

        confirmed_leaves = self.env['hr.leave'].sudo().search([
            ('employee_id', '=', payslip.employee_id.id),
            ('state', '=', 'validate'),
            ('holiday_status_id.is_annual_leave', '=', True),
            ('x_return_state', '=', 'hr_confirmed'),
            ('x_return_date', '!=', False),
            ('request_date_from', '<=', payslip.date_to),
            ('request_date_to', '>=', payslip.date_from),
        ])

        # Exclude leaves where this payslip is one of the vacation payslips
        if payslip.id:
            confirmed_leaves = confirmed_leaves.filtered(
                lambda l: payslip.id not in l.x_vacation_payslip_ids.ids
            )

        if not confirmed_leaves:
            return None

        # Return the latest return date
        return max(l.x_return_date for l in confirmed_leaves)

    def _inject_prior_hra_input(self, payslip):
        """If another confirmed/done payslip exists for the same employee
        in the same month, inject PRIOR_HRA and PRIOR_GOSI input lines so
        the HRA/GOSI rules can subtract the already-paid amounts.  This
        prevents HRA and GOSI from being paid twice when a vacation payslip
        and a regular monthly payslip coexist.

        Also considers vacation payslips in 'draft' state that are linked
        to validated annual leaves (auto-generated, not yet confirmed).
        For cross-month vacations, only payslips whose date range actually
        overlaps this payslip's period are included.
        """
        if not payslip.employee_id or not payslip.date_from or not payslip.date_to:
            return

        # Remove any existing PRIOR_HRA / PRIOR_GOSI inputs (in case of recompute)
        existing_prior = payslip.input_line_ids.filtered(
            lambda i: i.code in ('PRIOR_HRA', 'PRIOR_GOSI')
        )
        if existing_prior:
            existing_prior.unlink()

        # Find other non-cancelled payslips for the same employee whose
        # period overlaps the same month.
        domain = [
            ('employee_id', '=', payslip.employee_id.id),
            ('state', 'in', ('verify', 'done')),
            ('date_from', '<=', payslip.date_to),
            ('date_to', '>=', payslip.date_from),
            # A revision re-states the whole period rather than adding to
            # it; its HRA/GOSI are the same ones this payslip already
            # carries, so counting them as "prior" would subtract twice.
            ('x_is_revision', '=', False),
        ]
        if payslip.id:
            domain.append(('id', '!=', payslip.id))

        prior_slips = self.env['hr.payslip'].sudo().search(domain)

        # Also include vacation payslips in 'draft' state that are linked
        # to validated annual leaves (auto-generated, not yet confirmed).
        # For cross-month vacations, only include payslips whose date range
        # actually overlaps this payslip's period.
        vacation_leaves = self.env['hr.leave'].sudo().search([
            ('employee_id', '=', payslip.employee_id.id),
            ('state', '=', 'validate'),
            ('holiday_status_id.is_annual_leave', '=', True),
            ('request_date_from', '<=', payslip.date_to),
            ('request_date_to', '>=', payslip.date_from),
        ])
        for leave in vacation_leaves:
            for vac_slip in leave.x_vacation_payslip_ids:
                if (vac_slip.state != 'cancel'
                        and not vac_slip.x_is_vacation_preview
                        and vac_slip.id != payslip.id
                        and vac_slip not in prior_slips
                        and vac_slip.date_from <= payslip.date_to
                        and vac_slip.date_to >= payslip.date_from):
                    prior_slips |= vac_slip

        if not prior_slips:
            return

        # Sum HRA and GOSI already paid in prior payslips
        prior_hra = 0.0
        prior_gosi = 0.0
        for slip in prior_slips:
            for line in slip.line_ids:
                if line.code == 'HRA' and line.total > 0:
                    prior_hra += line.total
                elif line.code == 'GOSI' and line.total < 0:
                    prior_gosi += line.total  # negative value

        version_id = (
            payslip.version_id.id
            or (payslip.employee_id.current_version_id
                and payslip.employee_id.current_version_id.id)
        )
        if not version_id:
            return

        slip_refs = ', '.join(prior_slips.mapped('number') or prior_slips.mapped('name'))

        if prior_hra > 0:
            self.env['hr.payslip.input'].sudo().create({
                'payslip_id': payslip.id,
                'version_id': version_id,
                'name': 'HRA already paid in %s' % slip_refs,
                'code': 'PRIOR_HRA',
                'amount': prior_hra,
                'sequence': 5,
            })

        if prior_gosi < 0:
            # Store as positive so the GOSI rule can add it back:
            # result = min(gosi + prior_gosi_input, 0)
            self.env['hr.payslip.input'].sudo().create({
                'payslip_id': payslip.id,
                'version_id': version_id,
                'name': 'GOSI already paid in %s' % slip_refs,
                'code': 'PRIOR_GOSI',
                'amount': abs(prior_gosi),
                'sequence': 6,
            })

    # ------------------------------------------------------------------
    # Vacation-return guard
    # ------------------------------------------------------------------

    @api.model
    def _get_unresolved_vacation_leaves(self, employee_id, date_to,
                                        exclude_payslip_id=None):
        """Return validated annual-leave records whose return is still
        pending (x_return_state == 'on_vacation').

        :param employee_id: int — employee DB id
        :param date_to: date — payslip end date
        :param exclude_payslip_id: int|None — exclude leaves whose
               vacation payslip IS this payslip (avoids self-blocking)
        :return: hr.leave recordset (may be empty)
        """
        if not employee_id or not date_to:
            return self.env['hr.leave']
        unresolved = self.env['hr.leave'].sudo().search([
            ('employee_id', '=', employee_id),
            ('state', '=', 'validate'),
            ('holiday_status_id.is_annual_leave', '=', True),
            ('x_return_state', '=', 'on_vacation'),
            ('request_date_from', '<=', date_to),
        ])
        if exclude_payslip_id:
            unresolved = unresolved.filtered(
                lambda l: exclude_payslip_id not in l.x_vacation_payslip_ids.ids
            )
        return unresolved

    def _check_unresolved_vacation(self, payslip):
        """Raise ValidationError if the employee has an unresolved
        vacation return for the payslip period.

        Leaves whose vacation payslip IS the current payslip are excluded
        (so recomputing the vacation payslip itself doesn't block).
        """
        if not payslip.employee_id or not payslip.date_to:
            return
        unresolved = self._get_unresolved_vacation_leaves(
            payslip.employee_id.id,
            payslip.date_to,
            exclude_payslip_id=payslip.id,
        )
        if unresolved:
            details = '\n'.join(
                '  • %s (%s → %s)' % (
                    l.holiday_status_id.name,
                    l.request_date_from,
                    l.request_date_to,
                )
                for l in unresolved
            )
            raise ValidationError(_(
                "Cannot compute payslip for %(employee)s.\n\n"
                "The following annual leave(s) have not been fully "
                "confirmed (HR return confirmation is still pending):\n"
                "%(details)s\n\n"
                "Please resolve the vacation return before processing "
                "payroll.",
                employee=payslip.employee_id.name,
                details=details,
            ))

    # ------------------------------------------------------------------
    # Ensure worked-day lines exist (batch / programmatic creation)
    # ------------------------------------------------------------------

    def _ensure_worked_days(self, payslip, effective_from=None,
                            effective_to=None, absence_only=False):
        """Populate worked_days_line_ids when they are empty (e.g. batch
        payslip generation where the UI onchange never fires).

        When *effective_from* is given (e.g. a vacation return date), it
        replaces ``payslip.date_from`` so that only attendance **from that
        date onwards** is counted — pre-vacation days are not
        double-counted.

        When *effective_to* is given (e.g. a vacation/EOS payslip where
        the leave starts mid-month), the attendance window is capped at
        that date.  Post-leave calendar days (effective_to+1 through
        payslip.date_to) are injected as absent so ATTDED fires for them.

        Because salary rules always compute full-month amounts (e.g.
        BASIC = wage), the pre-return / post-leave calendar days must
        still appear as absent so ATTDED deducts the correct amount.
        """
        version_ids = (
            payslip.version_id.ids
            or self.get_versions(
                payslip.employee_id, payslip.date_from, payslip.date_to)
        )
        if not version_ids:
            return
        versions = self.env['hr.version'].browse(version_ids)
        start = effective_from or payslip.date_from
        end = effective_to if effective_to is not None else payslip.date_to
        ctx = self.with_context(ksw_absence_only=True) if absence_only else self
        wd_vals = ctx.get_worked_day_lines(versions, start, end)

        # When effective_from shifts the attendance window, add the
        # pre-return calendar days as absent so ATTDED fires correctly.
        if effective_from and wd_vals:
            pre_return_days = (effective_from - payslip.date_from).days
            if pre_return_days > 0:
                self._add_pre_return_absent_days(
                    wd_vals, pre_return_days, versions[:1],
                    period_date=payslip.date_from)

        # When effective_to caps the window, add the post-leave calendar
        # days (effective_to+1 → payslip.date_to) as absent.
        if effective_to is not None:
            period_start = max(effective_to,
                               payslip.date_from - timedelta(days=1))
            post_leave_days = (payslip.date_to - period_start).days
            if post_leave_days > 0:
                self._add_post_leave_absent_days(
                    wd_vals, post_leave_days, versions[:1],
                    period_date=payslip.date_from)

            # When the leave starts on or before the payslip's first day
            # (effective_to < date_from), get_worked_day_lines returns
            # nothing — the attendance window is empty.  Salary rules
            # require a WORK100 line to exist (0 days worked).
            if not any(v.get('code') == 'WORK100' for v in wd_vals):
                wd_vals.insert(0, {
                    'name': _('Worked Days'),
                    'sequence': 1,
                    'code': 'WORK100',
                    'number_of_days': 0.0,
                    'number_of_hours': 0.0,
                    'version_id': versions[:1].id,
                })

        if wd_vals:
            payslip.worked_days_line_ids = [
                (0, 0, v) for v in wd_vals
            ]

    def _add_pre_return_absent_days(self, wd_vals, pre_return_days, version,
                                    period_date=None):
        """Inject pre-return calendar days into ATT_ABS / ATT_DED lines.

        When the monthly payslip only counts attendance from the vacation
        return date, the days between ``payslip.date_from`` and the return
        date are *not* covered by any attendance.  Salary rules always
        fire at full-month amounts, so these days must appear as absent
        with a corresponding deduction so the employee doesn't receive a
        double salary.
        """
        v = version.employee_id.sudo().current_version_id or version
        base = (
            (v.wage or 0.0)
            + (v.travel_allowance or 0.0)
            + (v.mobile_allowance or 0.0)
            + (v.other_allowance or 0.0)
        )
        daily_rate = base / _days_in_month(period_date) if base else 0.0
        pre_return_deduction = round(daily_rate * pre_return_days)
        pre_return_hours = round(pre_return_days * DAILY_HOURS, 2)

        # Update or create ATT_ABS
        att_abs = next((d for d in wd_vals if d.get('code') == 'ATT_ABS'),
                       None)
        if att_abs:
            att_abs['number_of_days'] += pre_return_days
            att_abs['number_of_hours'] = round(
                att_abs.get('number_of_hours', 0) + pre_return_hours, 2)
            att_abs['amount'] = (att_abs.get('amount', 0)
                                 + pre_return_deduction)
        else:
            wd_vals.append({
                'name': _('Absent Days'),
                'sequence': 2,
                'code': 'ATT_ABS',
                'number_of_days': pre_return_days,
                'number_of_hours': pre_return_hours,
                'amount': pre_return_deduction,
                'version_id': version.id,
            })

        # Update or create ATT_DED
        if pre_return_deduction > 0:
            att_ded = next(
                (d for d in wd_vals if d.get('code') == 'ATT_DED'), None)
            if att_ded:
                att_ded['number_of_days'] += pre_return_days
                att_ded['amount'] = (att_ded.get('amount', 0)
                                     + pre_return_deduction)
            else:
                wd_vals.append({
                    'name': _('Attendance Deduction'),
                    'sequence': 15,
                    'code': 'ATT_DED',
                    'number_of_days': pre_return_days,
                    'number_of_hours': 0,
                    'amount': pre_return_deduction,
                    'version_id': version.id,
                })

        # Legacy compatibility: some payroll structures still reference
        # ``MISDAYS`` for the same deduction concept.
        if pre_return_deduction > 0:
            misdays = next((d for d in wd_vals if d.get('code') == 'MISDAYS'), None)
            if misdays:
                misdays['number_of_days'] += pre_return_days
                misdays['amount'] = (misdays.get('amount', 0)
                                     + pre_return_deduction)
            else:
                wd_vals.append({
                    'name': _('Missing Days'),
                    'sequence': 16,
                    'code': 'MISDAYS',
                    'number_of_days': pre_return_days,
                    'number_of_hours': pre_return_hours,
                    'amount': pre_return_deduction,
                    'version_id': version.id,
                })

    def _add_post_leave_absent_days(self, wd_vals, post_leave_days, version,
                                    period_date=None):
        """Inject post-leave calendar days into ATT_ABS / ATT_DED lines.

        For vacation and EOS payslips the leave starts mid-month.  Days
        from the leave start through payslip.date_to are not worked and
        must appear absent so ATTDED deducts for them.
        VACATION_BAL / EOS input lines compensate for those days.
        """
        v = version.employee_id.sudo().current_version_id or version
        base = (
            (v.wage or 0.0)
            + (v.travel_allowance or 0.0)
            + (v.mobile_allowance or 0.0)
            + (v.other_allowance or 0.0)
        )
        daily_rate = base / _days_in_month(period_date) if base else 0.0
        post_leave_deduction = round(daily_rate * post_leave_days)
        post_leave_hours = round(post_leave_days * DAILY_HOURS, 2)

        att_abs = next((d for d in wd_vals if d.get('code') == 'ATT_ABS'),
                       None)
        if att_abs:
            att_abs['number_of_days'] += post_leave_days
            att_abs['number_of_hours'] = round(
                att_abs.get('number_of_hours', 0) + post_leave_hours, 2)
            att_abs['amount'] = att_abs.get('amount', 0) + post_leave_deduction
        else:
            wd_vals.append({
                'name': _('Absent Days'),
                'sequence': 2,
                'code': 'ATT_ABS',
                'number_of_days': post_leave_days,
                'number_of_hours': post_leave_hours,
                'amount': post_leave_deduction,
                'version_id': version.id,
            })

        if post_leave_deduction > 0:
            att_ded = next(
                (d for d in wd_vals if d.get('code') == 'ATT_DED'), None)
            if att_ded:
                att_ded['number_of_days'] += post_leave_days
                att_ded['amount'] = (att_ded.get('amount', 0)
                                     + post_leave_deduction)
            else:
                wd_vals.append({
                    'name': _('Attendance Deduction'),
                    'sequence': 15,
                    'code': 'ATT_DED',
                    'number_of_days': post_leave_days,
                    'number_of_hours': 0,
                    'amount': post_leave_deduction,
                    'version_id': version.id,
                })

        if post_leave_deduction > 0:
            misdays = next(
                (d for d in wd_vals if d.get('code') == 'MISDAYS'), None)
            if misdays:
                misdays['number_of_days'] += post_leave_days
                misdays['amount'] = (misdays.get('amount', 0)
                                     + post_leave_deduction)
            else:
                wd_vals.append({
                    'name': _('Missing Days'),
                    'sequence': 16,
                    'code': 'MISDAYS',
                    'number_of_days': post_leave_days,
                    'number_of_hours': post_leave_hours,
                    'amount': post_leave_deduction,
                    'version_id': version.id,
                })

    # ------------------------------------------------------------------
    # get_worked_day_lines — main override
    # ------------------------------------------------------------------

    @api.model
    def get_worked_day_lines(self, versions, date_from, date_to):
        """Build worked-day lines from actual hr.attendance records.

        * Attendance-sheet employees  → attended / absent days only.
        * All other employees (biometric) → attended, absent, late, early,
          plus monetary deduction total.  When no attendance records exist
          for the period, all calendar days are counted as unpresented.
        """
        res = []
        d_from = (
            fields.Date.to_date(date_from) if isinstance(date_from, str)
            else date_from
        )
        d_to = (
            fields.Date.to_date(date_to) if isinstance(date_to, str)
            else date_to
        )

        for version in versions:
            employee = version.employee_id
            if not employee:
                continue

            # Effective period for this version within the payslip dates
            ver_start = version.contract_date_start
            ver_end = version.contract_date_end
            eff_from = max(d_from, ver_start) if ver_start else d_from
            eff_to = min(d_to, ver_end) if ver_end else d_to
            if eff_from > eff_to:
                continue

            attendances = self.env['hr.attendance'].sudo().search([
                ('employee_id', '=', employee.id),
                ('check_in', '>=', datetime.combine(eff_from, time.min)),
                ('check_in', '<=', datetime.combine(eff_to, time.max)),
            ])

            is_sheet = employee.sudo().x_is_attendance_sheet
            is_absence_only = self.env.context.get('ksw_absence_only')

            if is_sheet:
                res += self._worked_day_lines_sheet(
                    version, employee, attendances, eff_from, eff_to)
            elif is_absence_only:
                # Absence-only structures (e.g. executives): deduct only for
                # fully absent or unpresented days; late/early carry no penalty.
                res += self._worked_day_lines_executive(
                    version, employee, attendances, eff_from, eff_to)
            else:
                # Biometric employees — always use attendance-based logic.
                # When attendances is empty (e.g. future days, or employee
                # didn't check in), all calendar days are counted as
                # unpresented / absent.  We do NOT fall back to the
                # standard om_hr_payroll calendar logic because it relies
                # on attendance_ids which are empty in this project
                # (calendar_group_ids is used instead).
                res += self._worked_day_lines_biometric(
                    version, employee, attendances, eff_from, eff_to)

        return res

    # ------------------------------------------------------------------
    # Scheduled Saturday hours (for short-shift overtime reclassification)
    # ------------------------------------------------------------------

    def _scheduled_saturday_hours(self, calendar):
        """Net scheduled hours on Saturday (dayofweek '5') for a calendar:
        sum of work-line hours minus break-line hours across its groups.
        Returns 0.0 when Saturday has no schedule line.

        e.g. 'Standard 44 hours/week' → 3.0 (10:00-13:00),
             'Abdullah Mutawa Special Shift' → 4.0 (08:00-12:00).
        """
        if not calendar:
            return 0.0
        total = 0.0
        for group in calendar.calendar_group_ids:
            for line in group.line_ids:
                if line.dayofweek == '5':
                    span = (line.hour_to or 0.0) - (line.hour_from or 0.0)
                    if line.day_period == 'break':
                        total -= span
                    else:
                        total += span
        return max(0.0, total)

    # ------------------------------------------------------------------
    # Attendance-sheet employees (attended / absent only)
    # ------------------------------------------------------------------

    def _worked_day_lines_sheet(self, version, employee, attendances,
                                date_from, date_to):
        """For attendance-sheet employees only attended vs absent matters.
        No late / early-leave tracking."""
        lines = []

        attended_count = len(attendances)
        attended_hours = sum(a.worked_hours or 0.0 for a in attendances)

        lines.append({
            'name': _('Attended Days'),
            'sequence': 1,
            'code': 'WORK100',
            'number_of_days': attended_count,
            'number_of_hours': round(attended_hours, 2),
            'version_id': version.id,
        })

        # --- Absent days ---
        # Prefer the attendance-sheet lines for accuracy; fall back to a
        # simple calendar-days-minus-attended calculation.
        sheet = self.env['ksw.attendance.sheet'].sudo().search([
            ('employee_id', '=', employee.id),
            ('month', '=', str(date_from.month)),
            ('year', '=', date_from.year),
        ], limit=1)

        calendar = self.env['ksw.attendance.sheet']._get_employee_calendar(
            employee)
        sat_required = bool(calendar and calendar.x_saturday_required)

        if sheet:
            absent_lines = sheet.line_ids.filtered(
                lambda l: date_from <= l.date <= date_to
                and not l.is_attended
            )
            absent_count = len(absent_lines)
            sat_absent_count = (
                len(absent_lines.filtered(lambda l: l.date.weekday() == 5))
                if sat_required else 0
            )
        else:
            calendar_days = (date_to - date_from).days + 1
            absent_count = max(0, calendar_days - attended_count)
            sat_absent_count = 0

        # NOTE: the Saturday short-shift overtime reclassification
        # (x_saturday_short_overtime) is deliberately NOT applied to
        # attendance-sheet employees — their days are marked manually by a
        # supervisor, not derived from real punches, so there is no short
        # Saturday shift to deduct-and-credit.  The full-day
        # x_saturday_required behaviour below is unchanged.

        if absent_count > 0:
            absent_hours = round(absent_count * DAILY_HOURS, 2)

            # Compute monetary deduction for absent days
            v = employee.sudo().current_version_id or version
            base = (
                (v.wage or 0.0)
                + (v.travel_allowance or 0.0)
                + (v.mobile_allowance or 0.0)
                + (v.other_allowance or 0.0)
            )
            daily_rate = base / _days_in_month(date_from) if base else 0.0
            deduction_total = round(daily_rate * absent_count)

            lines.append({
                'name': _('Absent Days'),
                'sequence': 2,
                'code': 'ATT_ABS',
                'number_of_days': absent_count,
                'number_of_hours': absent_hours,
                'amount': deduction_total,
                'version_id': version.id,
            })

            if deduction_total > 0:
                lines.append({
                    'name': _('Attendance Deduction'),
                    'sequence': 15,
                    'code': 'ATT_DED',
                    'number_of_days': absent_count,
                    'number_of_hours': 0,
                    'amount': deduction_total,
                    'version_id': version.id,
                })

                # Legacy compatibility alias for older payroll structures.
                lines.append({
                    'name': _('Missing Days'),
                    'sequence': 16,
                    'code': 'MISDAYS',
                    'number_of_days': absent_count,
                    'number_of_hours': 0,
                    'amount': deduction_total,
                    'version_id': version.id,
                })

            if sat_absent_count > 0:
                sat_credit = round(daily_rate * sat_absent_count)
                if sat_credit > 0:
                    lines.append({
                        'name': _('Saturday Overtime Credit'),
                        'sequence': 17,
                        'code': 'SAT_OT',
                        'number_of_days': sat_absent_count,
                        'number_of_hours': round(
                            sat_absent_count * DAILY_HOURS, 2),
                        'amount': sat_credit,
                        'version_id': version.id,
                    })

        return lines

    # ------------------------------------------------------------------
    # Executive / Absence-only employees
    # ------------------------------------------------------------------

    def _worked_day_lines_executive(self, version, employee, attendances,
                                    date_from, date_to):
        """For absence-only structures: a day is either attended (present,
        regardless of late arrival or early departure) or absent/unpresented
        (full-day deduction).  No partial deductions for late/early."""
        lines = []

        attended_dates = {a.check_in.date() for a in attendances if a.check_in}
        attended_count = len(attended_dates)
        attended_hours = sum(a.worked_hours or 0.0 for a in attendances)

        lines.append({
            'name': _('Worked Days'),
            'sequence': 1,
            'code': 'WORK100',
            'number_of_days': attended_count,
            'number_of_hours': round(attended_hours, 2),
            'version_id': version.id,
        })

        calendar_days = (date_to - date_from).days + 1
        absent_count = max(0, calendar_days - attended_count)

        if absent_count > 0:
            absent_hours = round(absent_count * DAILY_HOURS, 2)
            v = employee.sudo().current_version_id or version
            base = (
                (v.wage or 0.0)
                + (v.travel_allowance or 0.0)
                + (v.mobile_allowance or 0.0)
                + (v.other_allowance or 0.0)
            )
            daily_rate = base / _days_in_month(date_from) if base else 0.0
            deduction_total = round(daily_rate * absent_count)

            lines.append({
                'name': _('Absent Days'),
                'sequence': 2,
                'code': 'ATT_ABS',
                'number_of_days': absent_count,
                'number_of_hours': absent_hours,
                'amount': deduction_total,
                'version_id': version.id,
            })

            if deduction_total > 0:
                lines.append({
                    'name': _('Attendance Deduction'),
                    'sequence': 15,
                    'code': 'ATT_DED',
                    'number_of_days': absent_count,
                    'number_of_hours': 0,
                    'amount': deduction_total,
                    'version_id': version.id,
                })
                lines.append({
                    'name': _('Missing Days'),
                    'sequence': 16,
                    'code': 'MISDAYS',
                    'number_of_days': absent_count,
                    'number_of_hours': 0,
                    'amount': deduction_total,
                    'version_id': version.id,
                })

        return lines

    # ------------------------------------------------------------------
    # Biometric employees (full issue tracking)
    # ------------------------------------------------------------------

    def _worked_day_lines_biometric(self, version, employee, attendances,
                                    date_from, date_to):
        """For biometric employees: worked, absent, late, early-leave,
        unpresented days (no attendance record), and the aggregated
        monetary deduction."""
        lines = []

        non_absent = attendances.filtered(lambda a: not a.x_net_is_absent)
        absent = attendances.filtered('x_net_is_absent')
        late = attendances.filtered(lambda a: a.x_net_late_minutes > 0)
        early = attendances.filtered(
            lambda a: a.x_net_early_leave_minutes > 0)

        # --- Unpresented days (calendar days with no attendance record) ---
        attended_dates = {a.check_in.date() for a in attendances if a.check_in}
        total_calendar_days = (date_to - date_from).days + 1
        unpresented_count = max(0, total_calendar_days - len(attended_dates))

        # Daily rate for unpresented-day deduction (same formula as sheet)
        v = employee.sudo().current_version_id or version
        base = (
            (v.wage or 0.0)
            + (v.travel_allowance or 0.0)
            + (v.mobile_allowance or 0.0)
            + (v.other_allowance or 0.0)
        )
        daily_rate = base / _days_in_month(date_from) if base else 0.0
        unpresented_deduction = round(daily_rate * unpresented_count)

        # Saturday overtime credit for x_saturday_required calendars
        bio_calendar = self.env['ksw.attendance.sheet']._get_employee_calendar(
            employee)
        sat_required = bool(bio_calendar and bio_calendar.x_saturday_required)
        sat_absent_count = 0
        if sat_required:
            cur = date_from
            while cur <= date_to:
                if cur.weekday() == 5 and cur not in attended_dates:
                    sat_absent_count += 1
                cur += timedelta(days=1)

        # Saturday short-shift overtime reclassification (net-zero):
        # Saturday is scheduled <8h; deduct the (8h - actual Saturday hours)
        # gap and credit the same amount back as overtime.  Only Saturdays
        # the employee actually worked (short shift present) count.
        sat_short = bool(bio_calendar and bio_calendar.x_saturday_short_overtime)
        sat_short_amount = 0
        sat_short_count = 0
        sat_short_hours = 0.0
        if sat_short and daily_rate > 0:
            sched_sat = self._scheduled_saturday_hours(bio_calendar)
            shortfall_h = max(0.0, DAILY_HOURS - sched_sat)
            if shortfall_h > 0:
                cur = date_from
                while cur <= date_to:
                    if cur.weekday() == 5 and cur in attended_dates:
                        sat_short_count += 1
                    cur += timedelta(days=1)
                sat_short_hours = shortfall_h * sat_short_count
                sat_short_amount = round(
                    (daily_rate / DAILY_HOURS) * sat_short_hours)

        # WORK100 — Actually worked days
        lines.append({
            'name': _('Worked Days'),
            'sequence': 1,
            'code': 'WORK100',
            'number_of_days': len(non_absent),
            'number_of_hours': round(sum(
                a.x_net_worked_hours or 0.0 for a in non_absent), 2),
            'version_id': version.id,
        })

        # ATT_ABS — Absent days (record-based + unpresented)
        record_absent_count = len(absent)
        record_absent_deduction = round(sum(
            a.x_deduction_amount or 0.0 for a in absent))
        total_absent_count = record_absent_count + unpresented_count
        total_absent_deduction = record_absent_deduction + unpresented_deduction

        if total_absent_count > 0:
            lines.append({
                'name': _('Absent Days'),
                'sequence': 2,
                'code': 'ATT_ABS',
                'number_of_days': total_absent_count,
                'number_of_hours': round(total_absent_count * DAILY_HOURS, 2),
                'amount': total_absent_deduction,
                'version_id': version.id,
            })

        # Split non-absent deductions proportionally between late & early
        late_deduction_total = 0.0
        early_deduction_total = 0.0
        for a in non_absent:
            ded = a.x_deduction_amount or 0.0
            if ded <= 0:
                continue
            l_min = a.x_net_late_minutes or 0.0
            e_min = a.x_net_early_leave_minutes or 0.0
            total_min = l_min + e_min
            if total_min > 0:
                late_deduction_total += ded * l_min / total_min
                early_deduction_total += ded * e_min / total_min

        # ATT_LATE — Late arrivals
        if late:
            total_late_min = sum(a.x_net_late_minutes for a in late)
            lines.append({
                'name': _('Late Arrivals'),
                'sequence': 3,
                'code': 'ATT_LATE',
                'number_of_days': len(late),
                'number_of_hours': round(total_late_min / 60.0, 2),
                'amount': round(late_deduction_total),
                'version_id': version.id,
            })

        # ATT_EARLY — Early departures
        if early:
            total_early_min = sum(
                a.x_net_early_leave_minutes for a in early)
            lines.append({
                'name': _('Early Departures'),
                'sequence': 4,
                'code': 'ATT_EARLY',
                'number_of_days': len(early),
                'number_of_hours': round(total_early_min / 60.0, 2),
                'amount': round(early_deduction_total),
                'version_id': version.id,
            })

        # ATT_DED — Aggregated monetary deduction (records + unpresented +
        # the Saturday short-shift gap, which is credited back via SAT_OT).
        record_deduction = round(sum(
            a.x_deduction_amount or 0.0 for a in attendances))
        deduction_total = record_deduction + unpresented_deduction + sat_short_amount
        if deduction_total > 0:
            record_ded_days = len(
                attendances.filtered(
                    lambda a: (a.x_deduction_amount or 0.0) > 0))
            deduction_days = record_ded_days + unpresented_count + sat_short_count
            lines.append({
                'name': _('Attendance Deduction'),
                'sequence': 15,
                'code': 'ATT_DED',
                'number_of_days': deduction_days,
                'number_of_hours': 0,
                'amount': deduction_total,
                'version_id': version.id,
            })

            # Legacy compatibility alias for older payroll structures.
            lines.append({
                'name': _('Missing Days'),
                'sequence': 16,
                'code': 'MISDAYS',
                'number_of_days': deduction_days,
                'number_of_hours': 0,
                'amount': deduction_total,
                'version_id': version.id,
            })

        # Saturday overtime credit — full-day (x_saturday_required) and/or
        # short-shift (x_saturday_short_overtime) folded into one SAT_OT line,
        # 1:1 offset of the matching ATT_DED portion (net-zero).
        sat_credit = round(daily_rate * sat_absent_count) + sat_short_amount
        if sat_credit > 0:
            lines.append({
                'name': _('Saturday Overtime Credit'),
                'sequence': 17,
                'code': 'SAT_OT',
                'number_of_days': sat_absent_count + sat_short_count,
                'number_of_hours': round(
                    sat_absent_count * DAILY_HOURS + sat_short_hours, 2),
                'amount': sat_credit,
                'version_id': version.id,
            })

        return lines

    def get_deduction_breakdown(self):
        """Return a list of dicts for the payslip report's Attendance
        Deduction Breakdown section.  Includes both attendance records
        with deductions AND unpresented calendar days (no attendance
        record at all), so the breakdown total matches ATT_DED.
        """
        self.ensure_one()
        rows = []

        d_from = self.date_from
        d_to = self.date_to
        employee = self.employee_id
        if not employee or not d_from or not d_to:
            return rows

        # Daily rate for absent / unpresented days
        v = employee.sudo().current_version_id
        base = (
            (v.wage or 0.0)
            + (v.travel_allowance or 0.0)
            + (v.mobile_allowance or 0.0)
            + (v.other_allowance or 0.0)
        ) if v else 0.0
        daily_rate = base / _days_in_month(d_from) if base else 0.0

        day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday',
                     'Friday', 'Saturday', 'Sunday']

        # Check for vacation return — must mirror the worked-day logic
        return_date = self._get_vacation_return_date(self)
        eff_from = return_date if return_date else d_from

        # Pre-return days (vacation period) — all counted as absent
        if return_date and return_date > d_from:
            current = d_from
            while current < return_date:
                rows.append({
                    'date': current.strftime('%Y-%m-%d'),
                    'day': day_names[current.weekday()],
                    'late_min': 0,
                    'early_min': 0,
                    'is_absent': True,
                    'deduction': daily_rate,
                    'type': 'pre_return',
                })
                current += timedelta(days=1)

        # Attendance records with deductions (from effective start)
        absence_only = (self.struct_id and self.struct_id.x_absence_only_deduction)
        att_domain = [
            ('employee_id', '=', employee.id),
            ('check_in', '>=', datetime.combine(eff_from, time.min)),
            ('check_in', '<=', datetime.combine(d_to, time.max)),
            ('x_deduction_amount', '>', 0),
        ]
        if absence_only:
            att_domain.append(('x_net_is_absent', '=', True))
        att_recs = self.env['hr.attendance'].sudo().search(
            att_domain, order='check_in asc')

        for att in att_recs:
            if absence_only:
                ded = daily_rate
            elif att.x_net_is_absent:
                # For absent records, use the exact daily_rate (not the
                # rounded x_deduction_amount) so all absent rows are
                # consistent with unpresented-day rows.
                ded = daily_rate
            else:
                ded = att.x_deduction_amount or 0
            rows.append({
                'date': att.check_in.strftime('%Y-%m-%d'),
                'day': att.x_day_of_week or '',
                'late_min': 0 if absence_only else (att.x_net_late_minutes or 0),
                'early_min': 0 if absence_only else (att.x_net_early_leave_minutes or 0),
                'is_absent': att.x_net_is_absent,
                'deduction': ded,
                'type': 'absent' if (absence_only or att.x_net_is_absent) else 'issue',
            })

        # Unpresented days (from effective start, no attendance at all)
        all_att = self.env['hr.attendance'].sudo().search([
            ('employee_id', '=', employee.id),
            ('check_in', '>=', datetime.combine(eff_from, time.min)),
            ('check_in', '<=', datetime.combine(d_to, time.max)),
        ])
        attended_dates = {a.check_in.date() for a in all_att if a.check_in}

        # For attendance-sheet employees, Fridays (non-workdays) never get an
        # hr.attendance record even when paid (is_attended=True), so the
        # generic "no record → absent" loop would wrongly flag every Friday.
        # Instead, use the sheet lines: only days explicitly marked
        # is_attended=False are real absences.
        is_sheet = employee.sudo().x_is_attendance_sheet
        sheet_absent_dates = None
        if is_sheet:
            sheet = self.env['ksw.attendance.sheet'].sudo().search([
                ('employee_id', '=', employee.id),
                ('month', '=', str(eff_from.month)),
                ('year', '=', eff_from.year),
            ], limit=1)
            if sheet:
                sheet_absent_dates = {
                    l.date for l in sheet.line_ids
                    if eff_from <= l.date <= d_to and not l.is_attended
                }

        current = eff_from
        while current <= d_to:
            if current not in attended_dates:
                # Sheet employees: skip days the sheet considers attended
                # (e.g. Fridays with is_attended=True have no attendance
                # record but are not absences).
                if sheet_absent_dates is not None and current not in sheet_absent_dates:
                    current += timedelta(days=1)
                    continue
                rows.append({
                    'date': current.strftime('%Y-%m-%d'),
                    'day': day_names[current.weekday()],
                    'late_min': 0,
                    'early_min': 0,
                    'is_absent': True,
                    'deduction': daily_rate,
                    'type': 'unpresented',
                })
            current += timedelta(days=1)

        # Saturday short-shift overtime gap (present Saturdays scheduled <8h).
        # The (8h - actual) gap is folded into ATT_DED and credited back 1:1
        # as Saturday overtime (net zero); add matching rows so the breakdown
        # total reconciles with ATT_DED.  Biometric employees only — sheet
        # (supervisor-marked) employees are excluded, matching the worked-day
        # builder.
        calendar = self.env['ksw.attendance.sheet']._get_employee_calendar(
            employee)
        if (not is_sheet and calendar
                and calendar.x_saturday_short_overtime and daily_rate > 0):
            shortfall_h = max(
                0.0, DAILY_HOURS - self._scheduled_saturday_hours(calendar))
            if shortfall_h > 0:
                gap = (daily_rate / DAILY_HOURS) * shortfall_h
                current = eff_from
                while current <= d_to:
                    if current.weekday() == 5 and current in attended_dates:
                        rows.append({
                            'date': current.strftime('%Y-%m-%d'),
                            'day': day_names[current.weekday()],
                            'late_min': 0,
                            'early_min': 0,
                            'is_absent': False,
                            'deduction': gap,
                            'type': 'sat_short',
                        })
                    current += timedelta(days=1)

        # Sort by date
        rows.sort(key=lambda r: r['date'])
        return rows

    # ==================================================================
    # Revision payslips
    # ==================================================================
    #
    # Problem.  A payslip is confirmed and paid; the employee complains it
    # was short; the complaint turns out to be justified (a time-off
    # request approved late, a lost weekend grant, attendance that had not
    # synced yet).  Simply issuing a second payslip for the month does not
    # work: HRA and GOSI are per-period amounts that have already been
    # paid/deducted once, the loan installments have already been
    # collected, and nothing states the one number that matters — how much
    # more the employee is owed.
    #
    # A revision rebuilds the payslip the employee *should* have received
    # for the period, using today's data and including the deductions that
    # were legitimately collected, then subtracts everything already paid
    # via the PRIOR_NET rule.  Its NET line *is* the difference payable.

    def _overlapping_slips_domain(self, states=None):
        """Domain for other payslips of the same employee covering any part
        of this payslip's period.  ``self`` is always excluded."""
        self.ensure_one()
        domain = [
            ('employee_id', '=', self.employee_id.id),
            ('date_from', '<=', self.date_to),
            ('date_to', '>=', self.date_from),
        ]
        if states:
            domain.append(('state', 'in', list(states)))
        if self.id:
            domain.append(('id', '!=', self.id))
        return domain

    # ------------------------------------------------------------------
    # A period can only be confirmed once
    # ------------------------------------------------------------------

    def _check_duplicate_done_period(self):
        """Refuse to confirm a second payslip for a period that already has
        a confirmed one.

        Called from every route into ``done`` (``action_payslip_done`` goes
        through ``write``), because view-level guards and a single button
        are not the only way in — see pitfall #37.

        Three exemptions:

        * the slip being confirmed is a **revision** — that is the
          sanctioned way to re-issue a paid period;
        * **every** blocking slip is an auto-generated vacation / EOS
          payslip whose leave return has been confirmed by the employee's
          direct manager (``x_return_state == 'hr_confirmed'`` — the value
          is historically named for HR but is written by
          ``action_confirm_return_manager``).  The employee is back, so the
          rest of the month is genuinely owed as a separate payslip;
        * ``self.env.su`` — consistent with ``_check_payroll_manager``, so
          crons and migrations are not broken.
        """
        if self.env.su:
            return
        for slip in self:
            if slip.x_is_revision or not slip.employee_id:
                continue
            blocking = self.sudo().search(
                slip._overlapping_slips_domain(states=('done',)))
            if not blocking:
                continue
            unresolved = blocking.filtered(
                lambda s: not s._is_settled_vacation_payslip())
            if not unresolved:
                continue
            raise UserError(_(
                "A confirmed payslip already exists for %(employee)s "
                "covering %(date_from)s → %(date_to)s: %(slips)s.\n\n"
                "A period cannot be confirmed twice. If that payslip was "
                "wrong, open it and press \"Issue Revision\" — the revision "
                "recomputes the period with current data and pays only the "
                "difference.",
                employee=slip.employee_id.name,
                date_from=slip.date_from,
                date_to=slip.date_to,
                slips=', '.join(
                    s.number or s.name or str(s.id) for s in unresolved),
            ))

    def _is_settled_vacation_payslip(self):
        """True when this payslip is a definitive vacation / EOS payslip
        whose leave return has been confirmed by the direct manager."""
        self.ensure_one()
        leave = self.sudo().x_leave_id
        return bool(
            leave
            and not self.x_is_vacation_preview
            and leave.x_return_state == 'hr_confirmed'
        )

    # ------------------------------------------------------------------
    # Issue a revision
    # ------------------------------------------------------------------

    def action_issue_revision(self):
        """Create (or reopen) the revision payslip for this period."""
        self.ensure_one()
        self._check_payroll_officer(_('issue a payslip revision'))

        if self.state != 'done':
            raise UserError(_(
                'Only a confirmed payslip can be revised. This payslip is '
                'in state "%s".'
            ) % self.state)
        if self.credit_note:
            raise UserError(_('A refund payslip cannot be revised.'))
        if self.x_is_revision:
            raise UserError(_(
                'This payslip is itself a revision. Confirm it first, then '
                'revise it again if a further correction is needed.'))

        # One open revision at a time — reopen rather than fork the period.
        existing = self.sudo().search(
            self._overlapping_slips_domain(states=('draft', 'verify'))
            + [('x_is_revision', '=', True)], limit=1)
        if existing:
            return self._action_open_payslip(
                existing, _('Revision (already open)'))

        revision = self._create_revision_payslip()
        return self._action_open_payslip(revision, _('Payslip Revision'))

    def _create_revision_payslip(self):
        """Build the revision: same period and structure, the one-time
        inputs of every superseded payslip, the installments those payslips
        already collected (frozen), and a PRIOR_NET line for the total
        already paid."""
        self.ensure_one()
        Payslip = self.env['hr.payslip'].sudo()
        InputLine = self.env['hr.payslip.input'].sudo()

        prior_slips = self._revision_prior_slips()
        prior_net = sum(
            self._get_net_total(slip) for slip in prior_slips)
        slip_refs = ', '.join(
            s.number or s.name or str(s.id) for s in prior_slips)

        version = (
            self.version_id
            or self.employee_id.sudo().current_version_id
        )
        revision = Payslip.create({
            'employee_id': self.employee_id.id,
            'name': _('Revision — %(employee)s — %(month)s/%(year)s (of %(source)s)',
                      employee=self.employee_id.name,
                      month=self.date_from.month,
                      year=self.date_from.year,
                      source=self.number or self.name or self.id),
            'date_from': self.date_from,
            'date_to': self.date_to,
            'struct_id': self.struct_id.id,
            'version_id': version.id,
            'company_id': self.company_id.id,
            # Preserve the vacation/EOS link so the attendance window is
            # capped exactly as it was on the payslip being revised.
            'x_leave_id': self.sudo().x_leave_id.id,
            'x_is_revision': True,
            'x_revised_payslip_id': self.id,
            'x_prior_net_paid': prior_net,
        })

        input_vals = self._build_revision_inputs(
            revision, prior_slips, prior_net, slip_refs)
        if input_vals:
            InputLine.create(input_vals)

        revision.compute_sheet()

        self.sudo().message_post(
            body=Markup(
                '<strong>🧾 Revision issued</strong><br/>'
                '<b>Revision:</b> %(revision)s<br/>'
                '<b>Already paid this period:</b> %(paid).2f<br/>'
                '<b>By:</b> %(user)s'
            ) % {
                'revision': revision.name,
                'paid': prior_net,
                'user': self.env.user.name,
            },
            subtype_xmlid='mail.mt_note',
        )
        return revision

    def _revision_prior_slips(self):
        """Every payslip whose NET this revision must subtract: the payslip
        being revised, any vacation payslip for the same period, and any
        earlier confirmed revision.

        Including earlier revisions is what makes a revision-of-a-revision
        self-consistent — the second one subtracts the original *and* the
        first one, so its NET is again the outstanding difference.
        """
        self.ensure_one()
        others = self.sudo().search(
            self._overlapping_slips_domain(states=('verify', 'done')))
        return (self | others).sudo()

    @api.model
    def _get_net_total(self, payslip):
        net_lines = payslip.sudo().line_ids.filtered(lambda l: l.code == 'NET')
        return sum(net_lines.mapped('total'))

    def _build_revision_inputs(self, revision, prior_slips, prior_net,
                               slip_refs):
        """Input lines for a revision payslip.

        Three groups:

        1. **One-time inputs** carried over from every superseded payslip
           (VACATION_BAL, FLIGHT_TICKET, PENALTY, commissions, EOS…),
           summed per code — the revision re-states the whole period, so
           each of these must appear exactly once.
        2. **Frozen installments** ``KSW_DEDP_<line_id>`` for every
           installment the superseded payslips already collected.  The
           deliberate ``P`` keeps them clear of ``'KSW_DED_'``, which
           ``_inject_ksw_deduction_inputs``, ``_ksw_apply_deduction_priority``
           and ``_sync_deductions_on_done`` all key off — so they count
           towards the deserved net without the ledger being touched.
           Installments still *pending* are injected separately by
           KSW_deduction as ordinary ``KSW_DED_`` inputs and are collected
           out of the difference.
        3. **PRIOR_NET** — everything already paid for the period.
        """
        self.ensure_one()
        version_id = (
            revision.version_id.id
            or (self.employee_id.sudo().current_version_id
                and self.employee_id.sudo().current_version_id.id)
        )
        if not version_id:
            return []

        skip_codes = ('PRIOR_HRA', 'PRIOR_GOSI', 'PRIOR_NET')
        carried = {}  # code -> {'name': str, 'amount': float, 'sequence': int}
        for slip in prior_slips:
            for inp in slip.sudo().input_line_ids:
                code = inp.code or ''
                if not code or code in skip_codes or code.startswith('KSW_DED'):
                    continue
                entry = carried.setdefault(
                    code, {'name': inp.name, 'amount': 0.0,
                           'sequence': inp.sequence})
                entry['amount'] += inp.amount

        vals = [
            {
                'payslip_id': revision.id,
                'version_id': version_id,
                'name': entry['name'],
                'code': code,
                'amount': entry['amount'],
                'sequence': entry['sequence'],
            }
            for code, entry in carried.items()
        ]

        vals += self._revision_frozen_deduction_inputs(
            revision, prior_slips, version_id)

        if prior_net:
            vals.append({
                'payslip_id': revision.id,
                'version_id': version_id,
                'name': _('Already paid in %s') % slip_refs,
                'code': 'PRIOR_NET',
                'amount': prior_net,
                'sequence': 190,
            })
        return vals

    def _revision_frozen_deduction_inputs(self, revision, prior_slips,
                                          version_id):
        """Input values reproducing the installments the superseded payslips
        already collected.

        Hook only — ``ksw.deduction.line`` lives in KSW_deduction, which
        depends on this module.  It is also not yet in the registry while
        this module's own tests run, so it must never be referenced from
        here directly.  See the override in
        ``KSW_deduction/models/hr_payslip.py``.
        """
        return []

    def _action_open_payslip(self, payslip, name):
        return {
            'type': 'ir.actions.act_window',
            'name': name,
            'res_model': 'hr.payslip',
            'view_mode': 'form',
            'res_id': payslip.id,
            'target': 'current',
        }

    def action_open_revisions(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Revisions'),
            'res_model': 'hr.payslip',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self.x_revision_ids.ids)],
        }

    # ------------------------------------------------------------------
    # Over-payment: never pay a negative net, recover it instead
    # ------------------------------------------------------------------

    def _overpaid_revisions(self):
        """Revisions whose recomputation shows the employee was over-paid."""
        return self.filtered(
            lambda s: s.x_is_revision and s.x_net_wage < 0)

    def _handle_overpaid_revisions(self):
        """Cancel each over-paid revision and open a draft deduction to
        recover the difference.

        Deliberately does **not** raise: a ``UserError`` would roll the
        transaction back, taking the ``ksw.deduction`` we just created with
        it.  The batch-generation wizard uses the same
        cancel-and-notify shape.
        """
        recovered = []
        for slip in self:
            amount = abs(slip.x_net_wage)
            deduction = slip._create_overpayment_deduction(amount)
            slip.sudo().write({'state': 'cancel'})
            slip.sudo().message_post(
                body=Markup(
                    '<strong>⚠️ Over-payment — revision cancelled</strong><br/>'
                    'Recomputing this period shows the employee was '
                    '<b>over-paid by %(amount).2f</b>. A payslip cannot pay '
                    'a negative amount, so this revision was cancelled and a '
                    'recovery deduction was opened instead.<br/>'
                    '<b>Recovery deduction:</b> %(deduction)s<br/>'
                    '<b>By:</b> %(user)s'
                ) % {
                    'amount': amount,
                    'deduction': deduction.name if deduction
                                 else _('could not be created'),
                    'user': self.env.user.name,
                },
                subtype_xmlid='mail.mt_note',
            )
            recovered.append((slip, amount, deduction))

        details = '\n'.join(
            '• %s — %.2f (%s)' % (
                s.employee_id.name, amt,
                d.name if d else _('deduction not created — set the '
                                   'recovery type in Payroll Settings'))
            for s, amt, d in recovered
        )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Over-payment — %d revision(s) cancelled')
                         % len(recovered),
                'message': _(
                    'The recomputed period is lower than what was already '
                    'paid, so nothing is payable. A draft recovery '
                    'deduction was opened instead:\n%s'
                ) % details,
                'type': 'warning',
                'sticky': True,
            },
        }

    def _create_overpayment_deduction(self, amount):
        """Open a deduction recovering an over-payment.

        Hook only — ``ksw.deduction`` lives in KSW_deduction, which depends
        on this module, so the real implementation is the override in
        ``KSW_deduction/models/hr_payslip.py``.  Same shape as
        ``_create_vacation_payslip``, whose no-op hook sits in
        KSW_annual_leave.  Returning a falsy value is handled by the caller.
        """
        self.ensure_one()
        _logger.warning(
            'KSW_deduction is not installed — revision %s cancelled '
            'without a recovery deduction for %.2f.', self.id, amount)
        return None

    # ------------------------------------------------------------------
    # Reversal actions restricted to the Payroll Manager
    # ------------------------------------------------------------------

    def _check_payroll_officer(self, what):
        """Guard actions open to the whole payroll team.

        Issuing a revision is additive — it pays a shortfall rather than
        undoing a confirmed payslip — so it sits with the Officer tier, not
        the Manager tier that owns Cancel / Set to Draft / Refund.  The
        view-level ``groups=`` is cosmetic; this is the check that holds
        over RPC (pitfall #15).
        """
        if self.env.su:
            return
        if not self.env.user.has_group(PAYROLL_OFFICER_GROUP):
            raise UserError(_(
                'Only the payroll team may %s.'
            ) % what)

    def _check_payroll_manager(self, what):
        """Guard the three actions that undo a payslip.

        `om_hr_payroll` leaves "Cancel Payslip" / "Set to Draft" / "Refund"
        open to any `group_hr_payroll_user` and records no reason, so a
        confirmed payslip could be silently rejected after the batch was
        closed (KSWCO SLIP/11307, June 2026).  The view-level `groups=` is
        only cosmetic — this is the check that actually holds over RPC.
        """
        if self.env.su:
            return
        if not self.env.user.has_group(PAYROLL_MANAGER_GROUP):
            raise UserError(_(
                'Only a Payroll Manager may %s. Please ask the payroll '
                'administrator to do it.'
            ) % what)

    def action_payslip_cancel(self):
        self._check_payroll_manager(_('reject (cancel) a payslip'))
        return super().action_payslip_cancel()

    def action_payslip_draft(self):
        self._check_payroll_manager(_('reset a payslip to draft'))
        return super().action_payslip_draft()

    def refund_sheet(self):
        self._check_payroll_manager(_('refund a payslip'))
        return super().refund_sheet()

    # ------------------------------------------------------------------
    # Auto-email payslip PDF on confirmation
    # ------------------------------------------------------------------

    def action_payslip_done(self):
        # Revisions carry the over-payment check.  Recompute them first so
        # the decision is made on final figures, not on whatever was on
        # screen when the officer opened the form.
        revisions = self.filtered('x_is_revision')
        overpaid = self.env['hr.payslip']
        notification = None
        if revisions:
            revisions.compute_sheet()
            overpaid = revisions._overpaid_revisions()
            if overpaid:
                notification = overpaid._handle_overpaid_revisions()

        payable = self - overpaid
        if not payable:
            return notification
        res = super(HrPayslip, payable).action_payslip_done()
        payable._send_auto_payslip_email()
        if not self.env.context.get('_ksw_skip_bank_refresh'):
            runs = payable.mapped('payslip_run_id').filtered(bool)
            if runs:
                runs._refresh_bank_totals()
        # A partly over-paid batch: the warning is the more useful return.
        return notification or res

    def write(self, vals):
        # Guard both routes into `done` from one place — `action_payslip_done`
        # lands here too (pitfall #37).
        if vals.get('state') == 'done':
            entering = self.filtered(lambda s: s.state != 'done')
            if entering:
                entering._check_duplicate_done_period()
                entering._check_revision_payable()
        res = super().write(vals)
        if 'payslip_run_id' in vals:
            runs = self.mapped('payslip_run_id').filtered(bool)
            if runs:
                runs._refresh_bank_totals()
        return res

    def _check_revision_payable(self):
        """Backstop for the raw ``write({'state': 'done'})`` RPC route.

        ``action_payslip_done`` handles an over-paid revision gracefully
        (cancel + recovery deduction + notification).  A direct write
        cannot return a notification, so it simply refuses — the outcome
        that matters is the same: a negative net is never confirmed.
        """
        if self.env.su:
            return
        overpaid = self._overpaid_revisions()
        if overpaid:
            raise UserError(_(
                "This revision shows the employee was over-paid by "
                "%(amount).2f, so there is nothing to pay.\n\n"
                "Use the \"Confirm\" button on the revision instead — it "
                "opens a recovery deduction for the amount automatically.",
                amount=abs(overpaid[0].x_net_wage),
            ))

    def unlink(self):
        runs = self.mapped('payslip_run_id').filtered(bool)
        res = super().unlink()
        if runs:
            runs._refresh_bank_totals()
        return res

    def _send_auto_payslip_email(self):
        template = self.env.ref(
            'KSW_payroll.mail_template_payslip_auto', raise_if_not_found=False)
        if not template:
            return
        for slip in self:
            employee = slip.employee_id
            if slip.state == 'done' and employee.x_auto_send_payslip and employee.work_email:
                template.sudo().send_mail(
                    slip.id, force_send=False)
