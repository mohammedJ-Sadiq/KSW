from odoo import api, fields, models


class KswAnnualLeaveUnpaid(models.Model):
    """Extend the annual-leave accrual to deduct validated unpaid days.

    Unpaid leave days reduce the *effective service days* used in the
    two-tier accrual formula for the period [effective_start, today].
    Only unpaid leaves that start on or after effective_start are counted
    (leaves before the opening reset date don't affect post-reset accrual).
    """
    _inherit = 'ksw.annual.leave'

    def _get_unpaid_leave_days(self, employee_id, since_date=None):
        """Return total validated unpaid-leave calendar days for an employee.

        Args:
            employee_id: hr.employee id
            since_date: optional date — only count leaves starting on or
                after this date (used to exclude pre-reset unpaid leaves).
        """
        return sum(
            self._unpaid_days_of(leave)
            for leave in self._get_unpaid_leaves(employee_id, since_date)
        )

    @api.model
    def _unpaid_days_of(self, leave):
        """Days of one leave that do not count as service.

        An unpaid-type leave counts in full; an annual leave counts only the
        excess the employee accepted as unpaid.
        """
        days = 0.0
        if leave.holiday_status_id.is_unpaid_leave:
            days += leave.number_of_days
        if (leave.holiday_status_id.is_annual_leave
                and leave.x_excess_days_accepted
                and leave.x_unpaid_portion_days > 0):
            days += leave.x_unpaid_portion_days
        return days

    def _get_unpaid_leaves(self, employee_id, since_date=None):
        """Validated leaves whose days reduce service (sudo recordset).

        Shared by the annual accrual and the EOS service period, so both
        exclude exactly the same days. Measure each with _unpaid_days_of().
        """
        domain = [
            ('employee_id', '=', employee_id),
            ('state', '=', 'validate'),
            ('holiday_status_id.is_unpaid_leave', '=', True),
        ]
        if since_date:
            domain.append(('date_from', '>=', fields.Datetime.to_datetime(since_date)))

        unpaid_leaves = self.env['hr.leave'].sudo().search(domain)

        domain2 = [
            ('employee_id', '=', employee_id),
            ('state', '=', 'validate'),
            ('holiday_status_id.is_annual_leave', '=', True),
            ('x_excess_days_accepted', '=', True),
            ('x_unpaid_portion_days', '>', 0),
        ]
        if since_date:
            domain2.append(('date_from', '>=', fields.Datetime.to_datetime(since_date)))

        combined_leaves = self.env['hr.leave'].sudo().search(domain2)
        return unpaid_leaves | combined_leaves

    @api.depends(
        'employee_id',
        'employee_id.version_ids.contract_date_start',
        'employee_id.version_ids.date_version',
        'employee_id.version_ids.active',
        'x_opening_reset_date',
        'x_opening_extra_days',
    )
    def _compute_leave_data(self):
        """Override to subtract unpaid days from effective service days.

        Calls the parent to compute the base accrual (which already handles
        the opening reset date). Then, if the employee has any validated
        unpaid leaves that fall within the accrual period [effective_start,
        today], those days are subtracted from the effective service days
        before recomputing total_accrued_days.

        Only unpaid leaves that START on or after effective_start are counted
        so that pre-reset unpaid leave does not reduce post-reset accrual.
        """
        super()._compute_leave_data()

        today = fields.Date.context_today(self)

        for rec in self:
            if not rec.employee_id or not rec.joining_date:
                continue

            joining = rec.joining_date

            # Effective start: reset date (if set and after joining) else joining
            if rec.x_opening_reset_date:
                effective_start = max(rec.x_opening_reset_date, joining)
            else:
                effective_start = joining

            if effective_start > today:
                continue

            # Only count unpaid days that started on or after effective_start
            unpaid_days = self._get_unpaid_leave_days(
                rec.employee_id.id, since_date=effective_start
            )
            if unpaid_days <= 0:
                continue

            effective_days = (today - effective_start).days
            if effective_days <= 0:
                continue

            # Effective service days in the accrual period (unpaid excluded)
            service_days = max(effective_days - unpaid_days, 0)

            # Tier boundaries based on TOTAL service from joining date
            days_before_reset = (effective_start - joining).days
            five_years = 5 * 365

            tier1_effective = (
                min(days_before_reset + service_days, five_years)
                - min(days_before_reset, five_years)
            )
            tier1_effective = max(tier1_effective, 0)

            tier2_effective = (
                max(days_before_reset + service_days - five_years, 0)
                - max(days_before_reset - five_years, 0)
            )
            tier2_effective = max(tier2_effective, 0)

            extra_days = rec.x_opening_extra_days or 0.0

            rec.total_accrued_days = round(
                tier1_effective * (21.0 / 365.0)
                + tier2_effective * (30.0 / 365.0)
                + extra_days,
                4,
            )
