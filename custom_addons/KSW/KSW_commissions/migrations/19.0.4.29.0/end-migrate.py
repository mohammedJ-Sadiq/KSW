"""Only Mark Paid commits a commission month — re-capture what 19.0.4.28.0
left out.

19.0.4.28.0 treated an exported bank file as a payment, so a request
latched at Step 4 while that rule was live (KSWCO leave 5218, 4 Oct 2026)
left out a month that is approved but not Paid. That month is the
vacation's to pay. Re-latch those requests while they are still with
Accounting; past Step 4 the GM returns them and the accountant Refreshes.

end-migrate: the EOS flag the latch reads lives in KSW_eos_leave, which
loads after this module (gotcha #45).
"""
import logging

from markupsafe import Markup

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

EXPORT_RULE_LIVE_FROM = '2026-10-04 07:40:00'


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    leaves = env['hr.leave'].search([
        ('x_annual_approval_state', '=', 'pending_acc'),
        ('x_commission_latched_date', '>=', EXPORT_RULE_LIVE_FROM),
    ]).filtered(lambda l: l._settles_commission_entries(l))
    for leave in leaves:
        before = sum(leave.x_commission_snapshot_ids.filtered(
            'included').mapped('amount'))
        leave._latch_commission_entries()
        after = sum(leave.x_commission_snapshot_ids.filtered(
            'included').mapped('amount'))
        leave.message_post(
            body=Markup(
                '<strong>Commissions re-captured</strong><br/>'
                'Only a month marked Paid is excluded from the settlement; '
                'approved months not yet paid are included again.<br/>'
                '<b>Before:</b> %(b).2f<br/><b>After:</b> %(a).2f'
            ) % {'b': before, 'a': after},
            subtype_xmlid='mail.mt_note',
        )
    _logger.info('KSW_commissions: re-latched %s request(s) at Step 4: %s',
                 len(leaves), leaves.ids)
