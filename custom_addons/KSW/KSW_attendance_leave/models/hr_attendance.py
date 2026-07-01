# -*- coding: utf-8 -*-
from odoo import api, fields, models


class HrAttendance(models.Model):
    _inherit = 'hr.attendance'

    x_leave_ids = fields.Many2many(
        'hr.leave',
        'hr_leave_attendance_rel',
        'attendance_id',
        'leave_id',
        string='Related Leaves',
    )

    x_is_covered = fields.Boolean(
        string='Covered by Time Off',
        compute='_compute_is_covered',
        store=True,
        help="Indicates whether this attendance issue has been covered "
             "by an approved time-off request.",
    )

    # ── Net fields: raw value minus accepted time-off minutes ──

    x_net_late_minutes = fields.Float(
        string='Late Minutes',
        compute='_compute_net_minutes',
        store=True,
        help='Late minutes remaining after deducting approved time-off.',
    )

    x_net_early_leave_minutes = fields.Float(
        string='Early Leave Minutes',
        compute='_compute_net_minutes',
        store=True,
        help='Early leave minutes remaining after deducting approved time-off.',
    )

    x_net_is_absent = fields.Boolean(
        string='Is Absent',
        compute='_compute_net_minutes',
        store=True,
        help='True if the employee is absent and not fully covered by an approved time-off.',
    )

    x_net_worked_hours = fields.Float(
        string='Worked Hours',
        compute='_compute_net_minutes',
        store=True,
        help='Worked hours including time covered by approved time-off.',
    )

    @api.depends('x_leave_ids', 'x_leave_ids.state')
    def _compute_is_covered(self):
        for att in self:
            att.x_is_covered = bool(
                att.x_leave_ids.filtered(lambda l: l.state == 'validate'))

    @api.depends(
        'x_late_minutes', 'x_early_leave_minutes', 'x_is_absent', 'worked_hours',
        'employee_id.x_check_in_only',
        'x_leave_ids.state',
        'x_leave_ids.x_attendance_line_ids.accepted_minutes',
        'x_leave_ids.x_attendance_line_ids.issue_type',
        'x_leave_ids.x_attendance_line_ids.attendance_id',
    )
    def _compute_net_minutes(self):
        for att in self:
            # Sum accepted minutes from approved leaves for this attendance
            accepted_late = 0.0
            accepted_early = 0.0
            absent_covered = False
            covering_leave = None

            approved_leaves = att.x_leave_ids.filtered(
                lambda l: l.state == 'validate'
            )
            for leave in approved_leaves:
                # Check if this leave covers absence
                if att.x_is_absent and not absent_covered:
                    if leave.x_attendance_ids and att.id in leave.x_attendance_ids.ids:
                        absent_covered = True
                        covering_leave = leave

                # Sum accepted minutes per issue type for THIS attendance record
                for line in leave.x_attendance_line_ids:
                    if line.attendance_id.id == att.id:
                        if line.issue_type == 'late':
                            accepted_late += line.accepted_minutes
                        elif line.issue_type == 'early_leave':
                            accepted_early += line.accepted_minutes

            att.x_net_late_minutes = max(0.0, att.x_late_minutes - accepted_late)
            if att.employee_id.x_check_in_only:
                att.x_net_early_leave_minutes = 0.0
            else:
                att.x_net_early_leave_minutes = max(0.0, att.x_early_leave_minutes - accepted_early)
            att.x_net_is_absent = att.x_is_absent and not absent_covered
            # Net worked hours = raw worked hours + accepted time-off hours
            total_accepted_hours = (accepted_late + accepted_early) / 60.0
            # For absent days covered by leave, add full scheduled hours
            absent_covered_hours = 0.0
            if absent_covered and covering_leave:
                check_date = att.check_in.date() if att.check_in else None
                absent_covered_hours = covering_leave._get_daily_work_hours(
                    att.employee_id, check_date
                ) or 8.0
            att.x_net_worked_hours = (att.worked_hours or 0.0) + total_accepted_hours + absent_covered_hours

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        absent_new = records.filtered(lambda a: a.x_is_absent and a.check_in and a.employee_id)
        if absent_new:
            absent_new._auto_link_regular_leave_coverage()
        return records

    def _auto_link_regular_leave_coverage(self):
        """Link new absence records to any validated regular leave already covering the date.

        Called from create() so that biometric-sync absences that arrive while
        a regular leave (sick, business trip, etc.) is already approved get
        their x_leave_ids populated immediately — without waiting for the user
        to manually re-open the leave form.

        Absence check_in is at midnight UTC; leave.date_from is work-start UTC.
        We compare by expanding leave bounds to full UTC days.
        """
        HrLeave = self.env['hr.leave'].sudo()
        from datetime import timedelta

        emp_atts = {}
        for att in self:
            emp_atts.setdefault(att.employee_id.id, []).append(att)

        for emp_id, atts in emp_atts.items():
            check_ins = [a.check_in for a in atts]
            max_ci = max(check_ins).replace(hour=23, minute=59, second=59)
            min_ci = min(check_ins).replace(hour=0, minute=0, second=0)
            leaves = HrLeave.search([
                ('employee_id', '=', emp_id),
                ('state', '=', 'validate'),
                ('holiday_status_id.is_attendance_issue', '=', False),
                ('date_from', '<=', max_ci),
                ('date_to', '>=', min_ci),
            ])
            if not leaves:
                continue
            for att in atts:
                att_sod = att.check_in.replace(hour=0, minute=0, second=0)
                att_eod = att.check_in.replace(hour=23, minute=59, second=59)
                covering = leaves.filtered(
                    lambda l: l.date_from <= att_eod and l.date_to >= att_sod
                )
                if covering:
                    covering[0].with_context(tracking_disable=True).write({
                        'x_attendance_ids': [(4, att.id)]
                    })

    def _compute_display_name(self):
        for rec in self:
            date_str = rec.check_in.strftime('%Y-%m-%d') if rec.check_in else 'No Date'
            issues = []
            details = []

            if rec.x_net_is_absent:
                issues.append('Absent')
                details.append('Full Day')
            elif rec.x_is_absent and not rec.x_net_is_absent:
                issues.append('Absent (Covered)')
                details.append('Full Day')

            if rec.x_late_minutes > 0:
                if rec.x_net_late_minutes > 0:
                    issues.append('Late')
                    details.append(f'{rec.x_net_late_minutes:.0f}/{rec.x_late_minutes:.0f} min')
                else:
                    issues.append('Late (Covered)')
                    details.append(f'0/{rec.x_late_minutes:.0f} min')

            if rec.x_early_leave_minutes > 0:
                if rec.x_net_early_leave_minutes > 0:
                    issues.append('Early Leave')
                    details.append(f'{rec.x_net_early_leave_minutes:.0f}/{rec.x_early_leave_minutes:.0f} min')
                else:
                    issues.append('Early Leave (Covered)')
                    details.append(f'0/{rec.x_early_leave_minutes:.0f} min')

            issue_str = ', '.join(issues) if issues else 'No Issue'
            detail_str = ', '.join(details) if details else ''

            rec.display_name = f"{date_str} - {issue_str} - {detail_str}" if detail_str else f"{date_str} - {issue_str}"
