"""KSW Commissions — the period lock.

A commission period is *locked* once its monthly pay run
(``ksw.pay.run``) has been approved by the General Manager.
From that moment the figures have been signed off and, once exported,
paid — so nothing that feeds them may change.

The lesson of ``hr.leave`` (see CLAUDE.md gotcha #37) is that a finalised
record has more than one way out, and guarding the button you were shown
is not locking it.  This module therefore holds **one predicate** and
**one guard**, and every route that could mutate a locked period calls
the same guard:

  * ``ksw.pay.batch``   — create / write / unlink / submit / reset
  * ``ksw.pay.entry``   — create / write / unlink
  * ``ksw.pay.run``     — write / unlink / reopen

Two subtleties, both learned the hard way elsewhere in this codebase:

1. ``allow_su=False`` exists because several call sites write through
   ``sudo()`` after having checked authorisation themselves.  A guard
   that unconditionally exempts ``env.su`` (as it must, or crons and
   computes break) never sees those paths.  They must opt out.

2. Sheet **auto-creation** paths (``_ensure_current_period_sheets``, the
   source sync) must not raise — they run from crons and from unrelated
   ``hr.employee`` writes.  They call :func:`period_is_locked` directly
   and skip the locked period instead.
"""
from odoo import _, fields
from odoo.exceptions import UserError

# A run in one of these states freezes its whole period.
LOCKING_STATES = ('approved', 'paid')


def normalise_period(period):
    """Return ``period`` as the first day of its month, or ``False``."""
    if not period:
        return False
    return fields.Date.to_date(period).replace(day=1)


def period_is_locked(env, period):
    """True when an approved or paid pay run covers ``period``.

    Never raises — safe to call from crons, computes and auto-create
    paths that need to *skip* rather than fail.
    """
    period = normalise_period(period)
    if not period:
        return False
    return bool(env['ksw.pay.run'].sudo().search_count([
        ('period', '=', period),
        ('state', 'in', LOCKING_STATES),
    ]))


def check_period_unlocked(env, period, what, allow_su=True):
    """Raise :class:`UserError` when ``period`` belongs to a locked run.

    :param what: what the caller was trying to do, e.g.
        ``"Editing this sheet"``.  Used verbatim in the message.
    :param allow_su: exempt ``env.su``.  Pass ``False`` from call sites
        that write through ``sudo()`` after their own authorisation
        check — otherwise the guard would never see them.

    The General Manager is exempt in every case: they are the one who
    can reopen the run, so blocking them would be a deadlock.
    """
    if allow_su and env.su:
        return
    if not period_is_locked(env, period):
        return
    if env.user.has_group('KSW_commissions.group_commission_gm'):
        return
    raise UserError(_(
        "The pay run for %(period)s has been approved and is locked. "
        "%(what)s is no longer possible. Ask the General Manager to "
        "reopen it.",
        period=normalise_period(period).strftime('%B %Y'),
        what=what,
    ))
