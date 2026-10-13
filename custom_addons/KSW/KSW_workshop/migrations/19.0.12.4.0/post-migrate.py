# -*- coding: utf-8 -*-
"""19.0.12.4.0: every Workshop Manager except Essam Jaber becomes a Reviewer.

User decision (Oct 2026): the Manager role triages the queue and is the one
told about new requests; the others only need to see and review the work.

Kept as Manager: Essam (matched by login, falling back to name) and the two
technical accounts, admin and OdooBot. If Essam is not found among the
managers nothing is moved at all: demoting every manager because a login was
spelt differently in this database would leave the queue with no one to
triage it.

Manager implies hr.group_hr_user. Group membership is explicit-only in
Odoo 19 (`group_ids`), so a demoted user who did not also hold HR Officer
directly loses it. That is intended (a reviewer needs no HR rights) but it
is logged by name, so it can be checked after the upgrade.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

KEEP_LOGIN = 'e.jaber@alkawthersw.com'


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    manager = env.ref('KSW_workshop.group_workshop_manager')
    reviewer = env.ref('KSW_workshop.group_workshop_reviewer')
    hr_user = env.ref('hr.group_hr_user')

    managers = manager.with_context(active_test=False).user_ids
    keep = managers.filtered(
        lambda u: u.login == KEEP_LOGIN
        or ('essam' in (u.name or '').lower() and 'jaber' in (u.name or '').lower()))
    if not keep:
        _logger.warning(
            'KSW_workshop: Essam Jaber is not a Workshop Manager here; '
            'no manager converted to Reviewer. Managers: %s', managers.mapped('login'))
        return

    for xmlid in ('base.user_admin', 'base.user_root'):
        keep |= env.ref(xmlid, raise_if_not_found=False) or env['res.users']

    to_convert = managers - keep
    to_convert.write({'group_ids': [(3, manager.id), (4, reviewer.id)]})
    _logger.info('KSW_workshop: kept as Workshop Manager: %s', keep.mapped('login'))
    _logger.info('KSW_workshop: converted to Workshop Reviewer: %s', to_convert.mapped('login'))
    lost_hr = to_convert.filtered(lambda u: hr_user not in u.all_group_ids)
    if lost_hr:
        _logger.warning(
            'KSW_workshop: no longer HR Officer (it came only through Workshop '
            'Manager): %s', lost_hr.mapped('login'))
