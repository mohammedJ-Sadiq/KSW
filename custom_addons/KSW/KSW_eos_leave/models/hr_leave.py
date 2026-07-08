import logging
from datetime import date, timedelta

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# EOS fields that only HR may write, enforced server-side.
_EOS_HR_FIELDS = frozenset({
    'x_eos_unpaid_days',
    'x_eos_termination_reason',
    'x_eos_previous_payments',
    'x_eos_notice_pay',
})


class HrLeave(models.Model):
    _inherit = 'hr.leave'

    # ------------------------------------------------------------------
    # Type flag — stored so it can be used safely in invisible expressions
    # ------------------------------------------------------------------

    x_is_eos_leave = fields.Boolean(
        string='Is EOS Leave',
        related='holiday_status_id.is_eos_leave',
        store=True,
        help='True when the leave type is flagged as an EOS request.',
    )

    # ------------------------------------------------------------------
    # HR-filled EOS fields (only writable by HR at pending_hr state)
    # ------------------------------------------------------------------

    x_eos_unpaid_days = fields.Float(
        string='Unpaid Vacations to be Deducted (Days)',
        digits=(10, 2),
        copy=False,
        help='Number of unpaid leave days taken during the service period. '
             'These days are excluded from the service period before recomputing '
             'the Article 84/85 EOS entitlement.',
    )
    x_eos_termination_reason = fields.Selection(
        selection=[
            ('84', 'Article 84 — Termination by Employer'),
            ('85', 'Article 85 — Resignation'),
        ],
        string='Termination Reason',
        copy=False,
        help='Determines which EOS formula applies. '
             'Article 84 = employer-initiated termination (full entitlement). '
             'Article 85 = employee resignation (tiered entitlement).',
    )
    x_eos_previous_payments = fields.Float(
        string='Previous Payments',
        digits=(16, 2),
        copy=False,
        help='Amount of EOS payments already made to the employee in prior '
             'instalments. Deducted from the EOS payslip.',
    )
    x_eos_notice_pay = fields.Float(
        string='Notice Pay (Deduction)',
        digits=(16, 2),
        copy=False,
        help='Notice period pay deducted from the EOS payslip '
             '(e.g. when the employee did not serve the notice period).',
    )

    # ------------------------------------------------------------------
    # Computed EOS amounts — base (unadjusted) reference values
    # ------------------------------------------------------------------

    x_eos_service_years = fields.Float(
        string='Base Service Years (Unadjusted)',
        compute='_compute_eos_base',
        digits=(5, 2),
    )
    x_eos_last_wage = fields.Float(
        string='Last Wage (SAR)',
        compute='_compute_eos_base',
        digits=(16, 2),
    )
    x_eos_termination_amount = fields.Float(
        string='Base EOS — Termination (Art. 84, Unadjusted)',
        compute='_compute_eos_base',
        digits=(16, 2),
    )
    x_eos_resignation_amount = fields.Float(
        string='Base EOS — Resignation (Art. 85, Unadjusted)',
        compute='_compute_eos_base',
        digits=(16, 2),
    )

    # ------------------------------------------------------------------
    # Computed EOS amounts — adjusted for unpaid days
    # ------------------------------------------------------------------

    x_eos_adjusted_service_years = fields.Float(
        string='Adjusted Service Years',
        compute='_compute_eos_adjusted',
        digits=(5, 2),
        help='Service years after subtracting unpaid leave days from the '
             'total service period.',
    )
    x_eos_adjusted_termination_amount = fields.Float(
        string='Adjusted EOS — Termination (Art. 84)',
        compute='_compute_eos_adjusted',
        digits=(16, 2),
        help='Article 84 EOS amount computed on the adjusted service years.',
    )
    x_eos_adjusted_resignation_amount = fields.Float(
        string='Adjusted EOS — Resignation (Art. 85)',
        compute='_compute_eos_adjusted',
        digits=(16, 2),
        help='Article 85 EOS amount computed on the adjusted service years.',
    )
    x_eos_payout_amount = fields.Float(
        string='EOS Payout Amount',
        compute='_compute_eos_payout',
        digits=(16, 2),
        help='Final EOS amount: the adjusted Art. 84 or Art. 85 figure, '
             'depending on the selected Termination Reason.',
    )

    # ------------------------------------------------------------------
    # EOS payslip link (hr.payslip model only available via KSW_payroll)
    # ------------------------------------------------------------------

    x_eos_payslip_id = fields.Many2one(
        'hr.payslip',
        string='EOS Payslip',
        readonly=True,
        copy=False,
        groups='om_hr_payroll.group_hr_payroll_user',
        help='The EOS payslip generated at GM Final Approval.',
    )

    # ------------------------------------------------------------------
    # Computations
    # ------------------------------------------------------------------

    @staticmethod
    def _eos_calc(total_days, wage):
        """Return (years, termination_amount, resignation_amount) for given days/wage."""
        years = total_days / 365.25
        first = min(years, 5.0)
        extra = max(years - 5.0, 0.0)
        term = 0.5 * wage * first + 1.0 * wage * extra
        if years < 2.0:
            resig = 0.0
        elif years < 5.0:
            resig = term / 3.0
        elif years < 10.0:
            resig = term * 2.0 / 3.0
        else:
            resig = term
        return years, term, resig

    @api.depends(
        'x_is_eos_leave',
        'employee_id',
        'employee_id.version_ids.contract_date_start',
        'employee_id.version_ids.active',
        'employee_id.current_version_id.wage',
    )
    def _compute_eos_base(self):
        today = fields.Date.context_today(self)
        for leave in self:
            if not leave.x_is_eos_leave or not leave.employee_id:
                leave.x_eos_service_years = 0.0
                leave.x_eos_last_wage = 0.0
                leave.x_eos_termination_amount = 0.0
                leave.x_eos_resignation_amount = 0.0
                continue
            emp = leave.employee_id.sudo()
            wage = emp.current_version_id.wage or 0.0
            versions = emp.version_ids.filtered(lambda v: v.contract_date_start)
            if not versions or not wage:
                leave.x_eos_service_years = 0.0
                leave.x_eos_last_wage = wage
                leave.x_eos_termination_amount = 0.0
                leave.x_eos_resignation_amount = 0.0
                continue
            joining = min(versions.mapped('contract_date_start'))
            total_days = max((today - joining).days, 0)
            years, term, resig = self._eos_calc(total_days, wage)
            leave.x_eos_service_years = years
            leave.x_eos_last_wage = wage
            leave.x_eos_termination_amount = term
            leave.x_eos_resignation_amount = resig

    @api.depends(
        'x_is_eos_leave',
        'x_eos_unpaid_days',
        'employee_id',
        'employee_id.version_ids.contract_date_start',
        'employee_id.version_ids.active',
        'employee_id.current_version_id.wage',
    )
    def _compute_eos_adjusted(self):
        today = fields.Date.context_today(self)
        for leave in self:
            if not leave.x_is_eos_leave or not leave.employee_id:
                leave.x_eos_adjusted_service_years = 0.0
                leave.x_eos_adjusted_termination_amount = 0.0
                leave.x_eos_adjusted_resignation_amount = 0.0
                continue

            emp = leave.employee_id.sudo()
            wage = emp.current_version_id.wage or 0.0
            versions = emp.version_ids.filtered(lambda v: v.contract_date_start)
            if not versions or not wage:
                leave.x_eos_adjusted_service_years = 0.0
                leave.x_eos_adjusted_termination_amount = 0.0
                leave.x_eos_adjusted_resignation_amount = 0.0
                continue

            joining = min(versions.mapped('contract_date_start'))
            total_days = max((today - joining).days, 0)
            adjusted_days = max(total_days - (leave.x_eos_unpaid_days or 0.0), 0.0)
            years, term, resig = self._eos_calc(adjusted_days, wage)

            leave.x_eos_adjusted_service_years = years
            leave.x_eos_adjusted_termination_amount = term
            leave.x_eos_adjusted_resignation_amount = resig

    @api.depends(
        'x_eos_termination_reason',
        'x_eos_adjusted_termination_amount',
        'x_eos_adjusted_resignation_amount',
    )
    def _compute_eos_payout(self):
        for leave in self:
            if leave.x_eos_termination_reason == '84':
                leave.x_eos_payout_amount = leave.x_eos_adjusted_termination_amount
            elif leave.x_eos_termination_reason == '85':
                leave.x_eos_payout_amount = leave.x_eos_adjusted_resignation_amount
            else:
                leave.x_eos_payout_amount = 0.0

    # ------------------------------------------------------------------
    # Write guard: EOS financial fields are HR-only
    # Sync: EOS leaves always have request_date_to == request_date_from
    # ------------------------------------------------------------------

    @api.onchange('request_date_from', 'x_is_eos_leave')
    def _onchange_eos_sync_dates(self):
        """Keep request_date_to == request_date_from for EOS leaves."""
        for leave in self:
            if leave.x_is_eos_leave and leave.request_date_from:
                leave.request_date_to = leave.request_date_from

    def create(self, vals_list):
        """For EOS leave types, force request_date_to == request_date_from."""
        if isinstance(vals_list, dict):
            vals_list = [vals_list]
        eos_type_ids = set(
            self.env['hr.leave.type'].search(
                [('is_eos_leave', '=', True)]).ids
        )
        for vals in vals_list:
            if (vals.get('holiday_status_id') in eos_type_ids
                    and vals.get('request_date_from')):
                vals['request_date_to'] = vals['request_date_from']
        return super().create(vals_list)

    def write(self, vals):
        eos_written = _EOS_HR_FIELDS & set(vals)
        if eos_written:
            for leave in self:
                if not leave.x_is_eos_leave or self.env.su:
                    continue
                if not self.env.user.has_group(
                        'KSW_annual_leave.group_annual_leave_hr'):
                    raise UserError(_(
                        'Only HR Approvers can fill EOS financial fields.'))
        return super().write(vals)

    # ------------------------------------------------------------------
    # DM step: suppress attendance-sheet wizard for EOS leaves
    # ------------------------------------------------------------------

    def action_dm_approve(self):
        result = super().action_dm_approve()
        # EOS leaves don't need attendance-sheet marking — the employee is
        # leaving, not going on vacation.
        if (
            len(self) == 1
            and self.x_is_eos_leave
            and isinstance(result, dict)
            and result.get('res_model') == 'ksw.leave.attendance.wizard'
        ):
            return True
        return result

    # ------------------------------------------------------------------
    # Payslip hook: route EOS leaves to _create_eos_payslip
    # ------------------------------------------------------------------

    def _create_vacation_payslip(self):
        eos = self.filtered('x_is_eos_leave')
        if eos:
            eos._create_eos_payslip()
        regular = self - eos
        if regular:
            # Call the KSW_payroll implementation for non-EOS leaves
            super(HrLeave, regular)._create_vacation_payslip()

    def _create_eos_payslip(self):
        """Create a final-month payslip including EOS components.

        The payslip includes the employee's regular salary for the final
        month PLUS:
          EOS_AMOUNT       — the chosen Art. 84 / Art. 85 payout
          EOS_PREV_PAYMENTS — previous EOS payments (deduction)
          EOS_NOTICE_PAY   — notice period deduction
        """
        Payslip = self.env['hr.payslip'].sudo()
        today = fields.Date.context_today(self)

        for leave in self:
            employee = leave.employee_id
            if not employee:
                continue

            version = employee.current_version_id
            if not version:
                _logger.warning(
                    'No active version (contract) for employee %s — '
                    'skipping EOS payslip creation.',
                    employee.name,
                )
                continue

            structure = version.struct_id
            if not structure:
                _logger.warning(
                    'No salary structure for employee %s — '
                    'skipping EOS payslip creation.',
                    employee.name,
                )
                continue

            # Same month-selection logic as vacation payslip: prefer current
            # month, but fall back to previous month if it has not yet been
            # settled (avoids all-absent attendance deduction on a blank month).
            month_start = today.replace(day=1)

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
                month_start = prev_month_start
                month_end = prev_month_end
            elif month_start.month == 12:
                month_end = date(month_start.year + 1, 1, 1) - timedelta(days=1)
            else:
                month_end = date(month_start.year, month_start.month + 1, 1) - timedelta(days=1)

            payslip = Payslip.create({
                'employee_id': employee.id,
                'name': 'EOS Payslip — %s — %s/%s' % (
                    employee.name, month_start.year, month_start.month),
                'date_from': month_start,
                'date_to': month_end,
                'struct_id': structure.id,
                'version_id': version.id,
                'x_leave_id': leave.id,
            })

            input_vals = self._build_eos_input_lines(leave, payslip)
            if input_vals:
                self.env['hr.payslip.input'].sudo().create(input_vals)

            payslip.compute_sheet()

            _logger.info(
                'EOS payslip #%s created for employee %s '
                '(leave #%s, month %s/%s).',
                payslip.id, employee.name, leave.id,
                month_start.year, month_start.month,
            )

            leave.sudo().write({'x_eos_payslip_id': payslip.id})

    @staticmethod
    def _build_eos_input_lines(leave, payslip):
        """Build hr.payslip.input values for EOS components."""
        vals_list = []
        version_id = payslip.version_id.id

        if leave.x_eos_payout_amount:
            reason_label = dict(
                leave._fields['x_eos_termination_reason'].selection
            ).get(leave.x_eos_termination_reason, '')
            vals_list.append({
                'payslip_id': payslip.id,
                'version_id': version_id,
                'name': 'EOS Amount (%s)' % reason_label,
                'code': 'EOS_AMOUNT',
                'amount': leave.x_eos_payout_amount,
            })

        if leave.x_eos_previous_payments:
            vals_list.append({
                'payslip_id': payslip.id,
                'version_id': version_id,
                'name': 'EOS Previous Payments',
                'code': 'EOS_PREV_PAYMENTS',
                'amount': leave.x_eos_previous_payments,
            })

        if leave.x_eos_notice_pay:
            vals_list.append({
                'payslip_id': payslip.id,
                'version_id': version_id,
                'name': 'EOS Notice Pay (Deduction)',
                'code': 'EOS_NOTICE_PAY',
                'amount': leave.x_eos_notice_pay,
            })

        return vals_list

    # ------------------------------------------------------------------
    # Cancel EOS payslip on refuse / reset-to-draft / back-to-approval
    # ------------------------------------------------------------------

    def _cancel_eos_payslip(self):
        """Cancel any EOS payslip linked to these leaves."""
        for leave in self:
            if leave.x_eos_payslip_id and leave.x_eos_payslip_id.state != 'cancel':
                leave.x_eos_payslip_id.sudo().write({'state': 'cancel'})
            if leave.x_eos_payslip_id:
                leave.sudo().write({'x_eos_payslip_id': False})

    def action_refuse(self):
        result = super().action_refuse()
        self.filtered('x_is_eos_leave')._cancel_eos_payslip()
        return result

    def _move_validate_leave_to_confirm(self):
        result = super()._move_validate_leave_to_confirm()
        self.filtered('x_is_eos_leave')._cancel_eos_payslip()
        return result

    def action_draft(self):
        result = super().action_draft()
        self.filtered('x_is_eos_leave')._cancel_eos_payslip()
        return result

    # ------------------------------------------------------------------
    # Validation: EOS leaves are not subject to working-day checks
    # ------------------------------------------------------------------

    def _get_leaves_on_public_holiday(self):
        # EOS termination requests don't need to fall on a working day
        non_eos = self.filtered(lambda l: not l.x_is_eos_leave)
        return super(HrLeave, non_eos)._get_leaves_on_public_holiday()

    # ------------------------------------------------------------------
    # Smart-button action: open EOS payslip
    # ------------------------------------------------------------------

    def action_open_eos_payslip(self):
        """Open the EOS payslip linked to this leave."""
        self.ensure_one()
        if not self.x_eos_payslip_id:
            raise UserError(_('No EOS payslip found for this leave.'))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'hr.payslip',
            'view_mode': 'form',
            'res_id': self.x_eos_payslip_id.id,
            'target': 'current',
        }
