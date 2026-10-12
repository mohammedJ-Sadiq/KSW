# -*- coding: utf-8 -*-
"""Give every named department GM the commission GM capability again.

Commission Role is a single-select, so a department manager who was also
named its GM (KSWCO: Taha, Equipment Rental) kept only Supervisor after his
user form was saved, and his department's handovers reached him with no
Approve button. Re-run the naming grant for every named GM; the commissions
override upgrades a Supervisor to "Supervisor & General Manager".
Idempotent and additive.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    gms = env['hr.department'].with_context(active_test=False).search([
        ('x_gm_id', '!=', False)]).mapped('x_gm_id')
    gms |= env['res.company'].search([]).mapped('x_default_gm_id')
    gm_group = env.ref('KSW_commissions.group_commission_gm')
    before = {u.id: u.has_group('KSW_commissions.group_commission_gm')
              for u in gms.user_id}
    env['hr.department']._ksw_grant_gm_capability(gms)
    fixed = gms.user_id.filtered(
        lambda u: not before[u.id] and gm_group in u.all_group_ids)
    _logger.info(
        "KSW_commissions 19.0.4.37.0: commission GM capability restored for "
        "%s named GM user(s): %s", len(fixed), ', '.join(fixed.mapped('login')))
