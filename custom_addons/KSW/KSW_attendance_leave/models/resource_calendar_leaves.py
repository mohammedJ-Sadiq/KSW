# -*- coding: utf-8 -*-
import logging
from datetime import timedelta

import pytz

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


# Changing any of these moves who / which days a public holiday covers.
_HOLIDAY_SCOPE_FIELDS = {
    'date_from', 'date_to', 'calendar_id', 'company_id', 'resource_id',
    'time_type',
}


class ResourceCalendarLeaves(models.Model):
    """A public holiday does not reach into a leave that is already running.

    Policy (KSW, Sep 2026): an employee who is already on annual or unpaid
    vacation on the day of a public holiday does not get that holiday.  His
    request stays exactly as approved; the holiday applies to everybody else.

    Core does the opposite.  ``resource.calendar.leaves.create()`` calls
    ``_reevaluate_leaves()``, whose whole purpose is to hand days back to the
    people who *are* on leave that day -- and the set it operates on is
    precisely "every leave overlapping the holiday", i.e. exactly the
    employees this policy exempts.  So the exemption is the no-op: skipping
    them leaves nothing for the pass to do.  Nobody else is touched by it,
    because the holiday is one row on the calendar and creates nothing
    per-employee.

    Three independent reasons this is also the only safe behaviour here, quite
    apart from the policy:

    1. It crashes.  The pass opens with an unguarded
       ``leaves.sudo().write({'state': 'confirm'})``
       (``addons/hr_holidays/models/resource.py``, line 69).  ``hr.leave.write()``
       runs ``_check_validity()`` whenever ``state`` is in the vals, and KSW
       accrues Annual Vacation daily, so an employee mid-vacation is routinely
       over-drawn and raises.  That propagates out of ``create()`` and rolls the
       whole holiday back -- the caller sees "There is no valid allocation to
       cover that request." about a record that has no allocation.  The
       per-leave loop further down was written to catch exactly this and never
       executes.  The scope is company + dates (``_get_domain`` never reads
       ``calendar_id``), so entering the holiday one schedule at a time fails
       identically.  See Odoo 19 Pitfalls #164.
    2. It would restamp durations.  The pass recomputes ``number_of_days``.
       For annual leave that is forbidden outright -- the duration depends on
       the balance at request time, so a recompute rewrites it with today's
       (Pitfall #33).  For unpaid leave the duration drives an arrears
       deduction, so every day trimmed is a day paid again (Pitfall #46).
    3. Its failure path refuses.  When ``_check_validity`` does raise inside
       the guarded loop, the leave is silently ``action_refuse()``d -- on a KSW
       request that can be sitting mid-chain in ``x_annual_approval_state``,
       which stays ``state == 'confirm'`` throughout and so looks untouched.

    And core's pass has nothing to be right about here: KSW grants the
    holiday itself (Sep 2026), the same way as the weekend grant -- a full
    scheduled-day ``hr.attendance`` row tagged ``x_public_holiday_id`` for
    every biometric employee who is not on leave that day.  Attendance-sheet
    employees are left to their sheet.  It is applied the moment the holiday
    is saved, re-derived on every edit / delete, re-run by the "Grant to
    Employees" button, and written by the absence generator for days that
    were still in the future.  See
    ``biometric.attendance.sync._sync_public_holiday_day``.
    """
    _inherit = 'resource.calendar.leaves'

    def _reevaluate_leaves(self, time_domain_dict):
        if not time_domain_dict:
            return
        # Log what we are exempting rather than dropping it silently: the
        # count is the audit trail for "why did nobody's balance move".
        skipped = self.env['hr.leave'].sudo().search_count(
            self._get_domain(time_domain_dict))
        if skipped:
            _logger.info(
                'KSW: public holiday leaves %s already-running leave request(s) '
                'untouched (employees already on leave do not receive the '
                'holiday).', skipped)
        return

    # ------------------------------------------------------------------
    # Granting the holiday
    # ------------------------------------------------------------------

    x_granted_attendance_ids = fields.One2many(
        'hr.attendance', 'x_public_holiday_id', string='Granted Days',
        readonly=True)
    x_granted_count = fields.Integer(
        string='Granted', compute='_compute_granted_count',
        help='Employee-days this public holiday has been granted to.')

    def _compute_granted_count(self):
        counts = dict(self.env['hr.attendance'].sudo()._read_group(
            [('x_public_holiday_id', 'in', self.ids)],
            ['x_public_holiday_id'], ['__count']))
        for holiday in self:
            holiday.x_granted_count = counts.get(holiday, 0)

    def _applies_to_employee(self, employee):
        """A public holiday (no resource) on the employee's company/schedule."""
        self.ensure_one()
        if self.resource_id or self.time_type != 'leave':
            return False
        employee = employee.sudo()
        if self.company_id and employee.company_id != self.company_id:
            return False
        if self.calendar_id:
            return self.calendar_id in (
                employee.main_calendar_id | employee.resource_calendar_id)
        return True

    def _local_days(self, employee):
        """The calendar dates this holiday covers in the employee's timezone."""
        self.ensure_one()
        tz = self.env['biometric.schedule.helper'].get_employee_tz(employee)
        first = pytz.utc.localize(self.date_from).astimezone(tz).date()
        # date_to is the holiday's last instant (23:59:59 local); the second
        # taken off keeps a midnight end from spilling onto the next day.
        last = pytz.utc.localize(
            self.date_to - timedelta(seconds=1)).astimezone(tz).date()
        day = first
        while day <= last:
            yield day
            day += timedelta(days=1)

    def _public_holidays(self):
        return self.filtered(lambda h: not h.resource_id)

    @api.model
    def _holiday_sync(self):
        # sudo: the sync writes hr.attendance, which a Time Off officer cannot.
        # The authority is the caller's right to edit the holiday itself.
        return self.env['biometric.attendance.sync'].sudo()

    def _granted_pairs(self):
        Sync = self._holiday_sync()
        return [
            (att.employee_id, Sync._local_date(att.employee_id, att.check_in))
            for att in self.sudo().x_granted_attendance_ids
        ]

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        holidays = records._public_holidays()
        if holidays:
            self._holiday_sync()._sync_public_holidays(holidays)
        return records

    def write(self, vals):
        if not _HOLIDAY_SCOPE_FIELDS.intersection(vals):
            return super().write(vals)
        old_pairs = self._granted_pairs()
        res = super().write(vals)
        holidays = self._public_holidays()
        if holidays or old_pairs:
            self._holiday_sync()._sync_public_holidays(
                holidays, extra_pairs=old_pairs)
        return res

    def unlink(self):
        pairs = self._granted_pairs()
        # Remove the grants before the holiday: ondelete='set null' would
        # otherwise leave them behind looking like real attendance.
        self.sudo().x_granted_attendance_ids.unlink()
        res = super().unlink()
        Sync = self._holiday_sync()
        today = fields.Date.context_today(self)
        for employee, day in pairs:
            if day < today:
                # Whatever the day is now: another holiday's grant, or the
                # absence the grant had replaced.
                Sync._check_absence_for_date(employee.sudo(), day)
        return res

    def action_grant_public_holiday(self):
        """Re-apply these holidays to every employee, now."""
        self.check_access('write')
        result = self._holiday_sync()._sync_public_holidays(
            self._public_holidays())
        message = _(
            '%(granted)s employee-day(s) granted, %(revoked)s revoked. '
            'Days still in the future are granted automatically on the day.',
            granted=result['granted'], revoked=result['revoked'])
        if result['failed']:
            message += ' ' + _(
                'Not granted (overlapping attendance, fix by hand): %(names)s',
                names=', '.join(sorted(set(result['failed_names']))))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Public holiday applied'),
                'message': message,
                'type': 'warning' if result['failed'] else 'success',
                'sticky': bool(result['failed']),
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }
