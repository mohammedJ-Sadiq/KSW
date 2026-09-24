"""KSW Commissions — the vacation hold.

When an employee goes on vacation the company settles his whole position
**at the request**, not at the end of the month: at the accounting step of
the leave chain the department accountant lists the commission months he is
still owed (``hr.leave.x_commission_line_ids``) and they ride out on the
vacation payslip, together with the balance and whatever he owes.  He walks
away with everything he had earned up to the day he left.

So the month he left in has already been paid, in full, on the leave — and
the monthly pay run must not pay it a second time.

This is the commissions twin of ``hr.payslip._check_unresolved_vacation``,
with one deliberate difference.  Payroll asks a **state** question — "has
anybody confirmed he is back?" — and that works there because a payslip is
computed inside the month it covers.  A commission month is prepared the
month after, or the month after that: an employee who left on 11 August and
came back on 17 September is long back by the time August is being prepared,
his return state has cleared, and a state question would wave through
exactly the month the settlement already paid.  The question has to be about
the **period**:

  * he left during the month            → the whole month was settled → hold
  * the month sits inside the vacation  → nothing was earned there    → hold
  * he came back during the month       → payable from the return date on
  * **he left AFTER the month ended, and the month had not been paid yet**
    → hold: it was still owed to him that day, so it went out with the
    settlement (a driver who left on 3 September was paid his August the
    same morning, and August is only being prepared now)

That last one is the case the period rule still missed on its first pass,
and it is the common one: the settlement pays **everything outstanding**,
not only the month he happened to leave in. Its bound is the month's own
pay run — once that run is approved or paid the money went out through
the register instead, so a later vacation settled nothing for it.

Three call sites, one predicate (the lesson of ``ksw_commission_lock``):
``ksw.pay.entry`` create/write, the BAS driver import, and the payment
register built by ``ksw.pay.run``.

The hold is a **policy, not a fact**: if Accounting never listed that month
on the leave then nobody has paid it, and holding it would simply lose the
employee his money.  So it can be released, per employee per month, with a
reason, on the record — ``ksw.pay.vacation.release``.
"""
import calendar
from collections import namedtuple
from datetime import timedelta

from odoo import _
from odoo.exceptions import UserError
from odoo.tools.misc import format_date

from .ksw_commission_lock import normalise_period, period_is_locked

#: ``kind`` is one of:
#:   'full'          — nothing in the month is payable
#:   'settled_later' — he left after the month ended, while it was still
#:                     owed to him, so the settlement paid it
#:   'partial'       — payable from ``payable_from`` onwards
#: ``leave`` is the request that caused it, so every message can name dates
#: the user recognises.
VacationHold = namedtuple('VacationHold', 'kind payable_from leave')

#: Which answer wins when two requests both speak for one month.  A
#: month he was actually away for explains itself better than one that
#: was merely settled later, and either outranks a part month.
_HOLD_RANK = {'partial': 1, 'settled_later': 2, 'full': 3}

#: Leave states that put a request on the hold's radar at all.  A refused,
#: cancelled or still-unapproved request has settled nothing.
_HELD_LEAVE_STATE = 'validate'


def month_bounds(period):
    """``(first_day, last_day)`` of ``period``'s month, or ``(False, False)``."""
    start = normalise_period(period)
    if not start:
        return False, False
    last = calendar.monthrange(start.year, start.month)[1]
    return start, start.replace(day=last)


def _away_until(leave):
    """The last day the employee was away — ``None`` while still away.

    ``x_return_date`` is authoritative in both directions: it is the day he
    actually walked back in, whether that is earlier than the planned end
    (KSW_annual_leave shortens the request to match) or later.  With no
    return date and the request still reading 'On Vacation', nobody has said
    he is back, so the vacation is open-ended — which is exactly the state
    the payroll guard refuses to compute a payslip against.
    """
    if leave.x_return_date:
        return leave.x_return_date - timedelta(days=1)
    if leave.x_return_state == 'on_vacation':
        return None
    return leave.request_date_to


def vacation_holds(env, employees, period):
    """``{employee_id: VacationHold}`` for whoever this month holds.

    One ``hr.leave`` search for the whole set, because every caller has a
    batch of employees in hand and a per-row search would be one query per
    entry on a screen that routinely carries forty.

    sudo() throughout: this is an *identity* read, not a scope one.  A
    supervisor has no ``hr.leave`` access at all, and whether his driver was
    away is not a question his record rules get a say in.
    """
    month_start, month_end = month_bounds(period)
    employee_ids = list(getattr(employees, 'ids', employees) or [])
    if not month_start or not employee_ids:
        return {}

    leaves = env['hr.leave'].sudo().search([
        ('employee_id', 'in', employee_ids),
        ('state', '=', _HELD_LEAVE_STATE),
        # No leave-type clause, for the same reason
        # hr.payslip._get_unresolved_vacation_leaves has none: x_return_state
        # only ever leaves 'not_applicable' on a type that settles at the
        # request — annual leave, and unpaid leave since Sep 2026 — so the
        # state IS the filter, and a type adopting the system later is
        # covered without touching this query again.
        ('x_return_state', '!=', 'not_applicable'),
        # Ends on or after the month began, OR is open-ended. The
        # second half is not redundant: a request whose planned end is
        # long past but which nobody has confirmed a return from is
        # still running, and it holds every month from its start on.
        # Dropping it is what stopped an unconfirmed August return
        # from holding September.
        '|',
        ('request_date_to', '>=', month_start),
        ('x_return_state', '=', 'on_vacation'),
    ], order='request_date_from')
    if not leaves:
        return {}

    # Has this month already gone out through its own register? If so,
    # no later settlement paid it and nothing here holds it.
    already_paid = period_is_locked(env, month_start)
    released = released_employee_ids(env, employee_ids, month_start)
    holds = {}
    for leave in leaves:
        employee_id = leave.employee_id.id
        if employee_id in released:
            continue
        candidate = _hold_for(leave, month_start, month_end, already_paid)
        if candidate is None:
            continue
        current = holds.get(employee_id)
        if current is None or _outranks(candidate, current):
            holds[employee_id] = candidate
    return holds


def _outranks(candidate, current):
    """Is `candidate` the better answer for this month than `current`?"""
    rank_new = _HOLD_RANK[candidate.kind]
    rank_old = _HOLD_RANK[current.kind]
    if rank_new != rank_old:
        return rank_new > rank_old
    if candidate.kind == 'partial':
        # Two part months: the later return is the binding one.
        return candidate.payable_from > current.payable_from
    return False


def _hold_for(leave, month_start, month_end, already_paid):
    """What one request says about one month, or ``None`` for nothing."""
    away_until = _away_until(leave)
    if away_until is not None and away_until < month_start:
        # The vacation was over before the month began. It settled what
        # was owed on ITS departure day, which is earlier still.
        return None
    if leave.request_date_from > month_end:
        # He left AFTER the month ended. The month was still owed to him
        # that day, so the settlement paid it — unless its own run had
        # already gone out, in which case there was nothing left to pay.
        if already_paid:
            return None
        return VacationHold('settled_later', None, leave)
    if (leave.request_date_from >= month_start
            or away_until is None
            or away_until >= month_end):
        # He left during the month (settled whole), or the month sits
        # entirely inside the vacation, or nobody has said he is back.
        return VacationHold('full', None, leave)
    # He was away from before the month and came back inside it.
    return VacationHold('partial', away_until + timedelta(days=1), leave)


def vacation_hold(env, employee, period):
    """The :class:`VacationHold` on one employee for one month, or ``None``."""
    return vacation_holds(env, employee, period).get(employee.id)


def released_employee_ids(env, employee_ids, period):
    """Employees the General Manager has released for ``period``.

    ``ksw_ignore_release_ids`` in the context pretends the named releases
    away.  Only ``ksw.pay.vacation.release`` itself uses it, to ask what it
    is about to release while its own row already exists.
    """
    period = normalise_period(period)
    employee_ids = list(employee_ids or [])
    if not period or not employee_ids:
        return set()
    domain = [('employee_id', 'in', employee_ids), ('period', '=', period)]
    ignore = list(env.context.get('ksw_ignore_release_ids') or ())
    if ignore:
        domain.append(('id', 'not in', ignore))
    releases = env['ksw.pay.vacation.release'].sudo().search_read(
        domain, ['employee_id'])
    return {r['employee_id'][0] for r in releases}


def hold_blocks(hold, entry_date=None):
    """Does ``hold`` stand in the way of something dated ``entry_date``?

    An undated occurrence in the month he came back is deliberately let
    through: meals and fixed allowances are a monthly figure with no day
    attached, so there is nothing to compare, and refusing them would cost a
    returning employee the part of the month he really did work.  The screen
    warns on those rows instead — see ``ksw.pay.entry.x_vacation_hold``.
    """
    if not hold:
        return False
    # Only a part month has a date to argue with. 'full' and
    # 'settled_later' both mean the whole month was paid elsewhere.
    # `not entry_date`, not `entry_date is None`: an unset Date field reads
    # back as False, which is what every caller actually passes.
    if hold.kind == 'partial' and (
            not entry_date or entry_date >= hold.payable_from):
        return False
    return True


def hold_reason(env, hold):
    """One line naming the vacation, for a warning banner or a summary."""
    leave = hold.leave
    if hold.kind == 'partial':
        return _(
            "on %(type)s until %(last_day)s — payable from %(return_date)s",
            type=leave.holiday_status_id.sudo().display_name,
            last_day=format_date(env, hold.payable_from - timedelta(days=1)),
            return_date=format_date(env, hold.payable_from),
        )
    if hold.kind == 'settled_later':
        return _(
            "went on %(type)s on %(start)s — this month was still owed to "
            "him then, so it went out with that settlement",
            type=leave.holiday_status_id.sudo().display_name,
            start=format_date(env, leave.request_date_from),
        )
    return _(
        "went on %(type)s on %(start)s — the month was settled on that "
        "request",
        type=leave.holiday_status_id.sudo().display_name,
        start=format_date(env, leave.request_date_from),
    )


def hold_message(env, employee, period, hold, what, entry_date=None):
    """The full explanation raised at a blocked route."""
    leave = hold.leave
    name = employee.sudo().display_name
    month = normalise_period(period).strftime('%B %Y')
    leave_type = leave.holiday_status_id.sudo().display_name
    if hold.kind == 'partial':
        return _(
            "%(employee)s was on %(type)s until %(last_day)s and came back "
            "on %(return_date)s.\n\n"
            "Everything up to the day he left was settled on that request, "
            "so only what he did from %(return_date)s onwards is payable in "
            "%(month)s — and this entry is dated %(date)s.\n\n"
            "%(what)s is therefore not possible.",
            employee=name, type=leave_type,
            last_day=format_date(env, hold.payable_from - timedelta(days=1)),
            return_date=format_date(env, hold.payable_from),
            month=month, date=format_date(env, entry_date), what=what,
        )
    if hold.kind == 'settled_later':
        return _(
            "%(employee)s went on %(type)s on %(start)s — after %(month)s "
            "had ended, but before %(month)s was ever paid.\n\n"
            "Everything still owed to him that day was settled on the "
            "request itself, and %(month)s was one of those months: "
            "Accounting lists them on the leave and they go out with the "
            "vacation payslip. So it has already been paid.\n\n"
            "%(what)s is therefore not possible.\n\n"
            "If %(month)s was in fact never listed on the leave, the "
            "General Manager can release it — Commissions → Vacation "
            "Releases — and it becomes payable again.",
            employee=name, type=leave_type,
            start=format_date(env, leave.request_date_from),
            month=month, what=what,
        )
    return _(
        "%(employee)s went on %(type)s on %(start)s.\n\n"
        "Everything he had earned up to that day was settled on the request "
        "itself: Accounting lists the commission months still owed on the "
        "leave and they are paid out with the vacation payslip. So "
        "%(month)s has already been paid and is not payable again here.\n\n"
        "%(what)s is therefore not possible.\n\n"
        "If that month was in fact never listed on the leave, the General "
        "Manager can release it — Commissions → Vacation Releases — and it "
        "becomes payable again.",
        employee=name, type=leave_type,
        start=format_date(env, leave.request_date_from),
        month=month, what=what,
    )


def check_not_held(env, employee, period, what,
                   entry_date=None, hold=None, allow_su=True):
    """Raise :class:`UserError` when ``period`` is held for ``employee``.

    :param what: what the caller was trying to do, e.g. ``"Adding an
        entry"`` — used verbatim in the message.
    :param hold: a hold already computed by the caller (batched lookups
        pass it in rather than paying for a second search).
    :param allow_su: exempt ``env.su``, as every other guard in this module
        does.  Pass ``False`` from a call site that writes through
        ``sudo()`` after checking authorisation itself.
    """
    if allow_su and env.su:
        return
    if hold is None:
        hold = vacation_hold(env, employee, period)
    if not hold_blocks(hold, entry_date):
        return
    raise UserError(hold_message(env, employee, period, hold, what,
                                 entry_date=entry_date))
