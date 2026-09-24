# -*- coding: utf-8 -*-
import logging

from odoo import models

_logger = logging.getLogger(__name__)


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

    And there is nothing for the pass to be right about in the first place: no
    KSW code reads ``resource.calendar.leaves``.  Neither workday oracle --
    ``biometric.schedule.helper.is_scheduled_workday()`` nor
    ``ksw.attendance.sheet._get_work_schedule()`` -- consults it, so a public
    holiday changes no absence row, no ATT_ABS day and no deduction (Pitfall
    #163).  It is a calendar annotation.
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
