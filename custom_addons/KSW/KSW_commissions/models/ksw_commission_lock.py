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


def gm_for_department(env, department):
    """The General Manager (res.users) whose monthly run a scope belongs to.

    The department's effective GM, else the company default GM — the same
    resolution ``ksw.pay.submission._compute_gm`` stores. Each GM has his
    own run per month (Oct 2026), so this is also *which* run.
    """
    gm = department.sudo().x_effective_gm_id if department else False
    gm = gm or env.company.sudo().x_default_gm_id
    return gm.sudo().user_id if gm else env['res.users']


def period_is_locked(env, period, gm=None):
    """True when an approved or paid pay run covers ``period``.

    Each General Manager has his own run per month, so ``gm`` says whose:
    one GM finalising his month must not freeze another's departments.
    ``gm=None`` keeps the old, conservative reading — any run of that
    month — for callers that cannot name a scope.

    Never raises — safe to call from crons, computes and auto-create
    paths that need to *skip* rather than fail.
    """
    period = normalise_period(period)
    if not period:
        return False
    domain = [('period', '=', period), ('state', 'in', LOCKING_STATES)]
    if gm is not None:
        domain.append(('gm_id', '=', gm.id or False))
    return bool(env['ksw.pay.run'].sudo().search_count(domain))


def check_period_unlocked(env, period, what, allow_su=True, gm=None):
    """Raise :class:`UserError` when ``period`` belongs to a locked run.

    :param what: what the caller was trying to do, e.g.
        ``"Editing this sheet"``.  Used verbatim in the message.
    :param gm: the General Manager whose run the caller belongs to (see
        :func:`period_is_locked`).
    :param allow_su: exempt ``env.su``.  Pass ``False`` from call sites
        that write through ``sudo()`` after their own authorisation
        check — otherwise the guard would never see them.

    The General Manager is exempt in every case: they are the one who
    can reopen the run, so blocking them would be a deadlock.
    """
    if allow_su and env.su:
        return
    if not period_is_locked(env, period, gm=gm):
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
