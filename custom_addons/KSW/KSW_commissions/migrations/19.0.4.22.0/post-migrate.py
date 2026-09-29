"""Latch the requests already with Accounting when the latch arrived.

The latch is taken when a request *enters* Step 4, so one already sitting
there would never get it and its accountant would be reviewing a live list
again. Take it now. Requests past Step 4 are deliberately left unlatched:
nobody reviewed commission entries for them, so their payslip pays none
(the accountant typed any commissions by hand, as before).
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    leaves = env['hr.leave'].search([
        ('x_annual_approval_state', '=', 'pending_acc'),
        ('x_commission_latched_date', '=', False),
    ]).filtered(lambda l: l._settles_commission_entries(l))
    leaves._latch_commission_entries()
    _logger.info('KSW_commissions: latched commission entries on %s request(s) '
                 'at the accounting step: %s', len(leaves), leaves.ids)
