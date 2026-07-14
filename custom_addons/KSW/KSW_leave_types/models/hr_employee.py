from datetime import timedelta

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    def _get_joining_date(self):
        """Return the earliest contract_date_start for this employee (same logic as KSW annual leave)."""
        self.ensure_one()
        versions = self.sudo().version_ids.filtered(lambda v: v.contract_date_start)
        if not versions:
            return False
        return min(versions.mapped('contract_date_start'))

    def _sick_leave_year_bounds(self, joining, today):
        """Return (year_start, year_end) for the current sick leave service year."""
        year_start = joining
        while year_start + relativedelta(years=1) <= today:
            year_start = year_start + relativedelta(years=1)
        year_end = year_start + relativedelta(years=1) - timedelta(days=1)
        return year_start, year_end

    def _create_sick_leave_allocations(self):
        """Create 120-day regular sick leave allocations for the current service year."""
        sick_type = self.env.ref('KSW_leave_types.leave_type_sick', raise_if_not_found=False)
        if not sick_type:
            return

        today = fields.Date.today()

        for emp in self:
            joining = emp._get_joining_date()
            if not joining or joining > today:
                continue

            year_start, year_end = emp._sick_leave_year_bounds(joining, today)

            # Skip if a non-refused allocation already covers this service year
            existing = self.env['hr.leave.allocation'].search([
                ('holiday_status_id', '=', sick_type.id),
                ('employee_id', '=', emp.id),
                ('state', '!=', 'refuse'),
                ('date_from', '>=', year_start),
            ], limit=1)
            if existing:
                continue

            year_label = f'{year_start.year}/{year_start.year + 1}' if year_start.month != 1 or year_start.day != 1 else str(year_start.year)
            alloc = self.env['hr.leave.allocation'].with_context(
                mail_create_nosubscribe=True,
                mail_notrack=True,
            ).create({
                'name': f'Sick Leave {year_label} — {emp.name}',
                'holiday_status_id': sick_type.id,
                'employee_id': emp.id,
                'allocation_type': 'regular',
                'number_of_days': 120,
                'date_from': year_start,
                'date_to': year_end,
            })
            alloc.action_approve()

    def _renew_expiring_sick_leave_allocations(self):
        """Create renewal allocations for sick leave expiring within the next 30 days."""
        sick_type = self.env.ref('KSW_leave_types.leave_type_sick', raise_if_not_found=False)
        if not sick_type:
            return

        today = fields.Date.today()
        renewal_window = today + relativedelta(days=30)

        expiring = self.env['hr.leave.allocation'].search([
            ('holiday_status_id', '=', sick_type.id),
            ('employee_id', 'in', self.ids),
            ('state', '=', 'validate'),
            ('allocation_type', '=', 'regular'),
            ('date_to', '>=', today),
            ('date_to', '<=', renewal_window),
        ])
        for alloc in expiring:
            new_date_from = alloc.date_to + timedelta(days=1)
            new_date_to = new_date_from + relativedelta(years=1) - timedelta(days=1)
            # Skip if a renewal already exists for the next period
            already_renewed = self.env['hr.leave.allocation'].search([
                ('holiday_status_id', '=', sick_type.id),
                ('employee_id', '=', alloc.employee_id.id),
                ('state', '!=', 'refuse'),
                ('date_from', '>=', new_date_from),
            ], limit=1)
            if already_renewed:
                continue
            year_label = f'{new_date_from.year}/{new_date_from.year + 1}' if new_date_from.month != 1 or new_date_from.day != 1 else str(new_date_from.year)
            new_alloc = self.env['hr.leave.allocation'].with_context(
                mail_create_nosubscribe=True,
                mail_notrack=True,
            ).create({
                'name': f'Sick Leave {year_label} — {alloc.employee_id.name}',
                'holiday_status_id': sick_type.id,
                'employee_id': alloc.employee_id.id,
                'allocation_type': 'regular',
                'number_of_days': 120,
                'date_from': new_date_from,
                'date_to': new_date_to,
            })
            new_alloc.action_approve()

    def _create_hajj_leave_allocations(self):
        """Create one-time 10-day Hajj leave allocation for employees with 2+ years of service."""
        hajj_type = self.env.ref('KSW_leave_types.leave_type_hajj', raise_if_not_found=False)
        if not hajj_type:
            return

        today = fields.Date.today()
        existing = self.env['hr.leave.allocation'].search([
            ('holiday_status_id', '=', hajj_type.id),
            ('employee_id', 'in', self.ids),
            ('state', '!=', 'refuse'),
        ])
        already_allocated_ids = set(existing.mapped('employee_id').ids)

        for emp in self:
            if emp.id in already_allocated_ids:
                continue
            joining = emp._get_joining_date()
            if not joining:
                continue
            service_days = (today - joining).days
            if service_days < 730:  # 2 years
                continue
            alloc = self.env['hr.leave.allocation'].with_context(
                mail_create_nosubscribe=True,
                mail_notrack=True,
            ).create({
                'name': f'Hajj Leave — {emp.name}',
                'holiday_status_id': hajj_type.id,
                'employee_id': emp.id,
                'allocation_type': 'regular',
                'number_of_days': 10,
                'date_from': today,
            })
            alloc.action_approve()

    @api.model
    def _cron_create_leave_allocations(self):
        """Daily cron: ensure every active employee has a current sick leave allocation.
        Renews expiring sick leave allocations. Checks Hajj eligibility on the 1st of each month."""
        employees = self.search([
            ('active', '=', True),
            ('employee_type', '=', 'employee'),
        ])
        employees._create_sick_leave_allocations()
        employees._renew_expiring_sick_leave_allocations()
        if fields.Date.today().day == 1:
            employees._create_hajj_leave_allocations()
