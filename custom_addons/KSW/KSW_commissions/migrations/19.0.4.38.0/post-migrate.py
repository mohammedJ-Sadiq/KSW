# -*- coding: utf-8 -*-
"""Move every open department submission onto its own GM's run.

The pre-migrate gave each run the GM most of its departments answer to;
any department under a different GM (KSWCO September 2026: Equipment
Rental, Taha) moves to that GM's run for the same month, created on the
spot. Approved submissions and finalised runs are not touched: that money
is settled. Idempotent.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    Sub = env['ksw.pay.submission']
    touched = Sub._rehome_all_open()
    mixed = env['ksw.pay.run'].search([]).filtered(
        lambda r: len(r.submission_ids.gm_id) > 1)
    _logger.info(
        "KSW_commissions 19.0.4.38.0: %s run(s) touched re-homing open "
        "submissions; %s run(s) still carry departments of more than one GM "
        "(finalised or approved, left as signed): %s",
        len(touched), len(mixed), ', '.join(mixed.mapped('display_name')))
