"""Meals and the location allowance are «بدل موقع» on the BAS voucher.

The accountant's term for both. With one description they are one column
in the bank Excel summary (it groups by description). In the journal they
stay one line each only where their accounts differ — Meals posts to
3201010006, the location allowance to 3201010005 — because merging two
accounts into one line would misstate the ledger.

The archived per-meal components are included: an old entry can still
name one. Only where the field is still empty.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

LABEL = 'بدل موقع'
XMLIDS = (
    'pay_component_meals',
    'pay_component_meal_breakfast',
    'pay_component_meal_lunch',
    'pay_component_meal_dinner',
    'pay_component_location_allowance',
)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    done = []
    for xmlid in XMLIDS:
        component = env.ref('KSW_commissions.%s' % xmlid,
                            raise_if_not_found=False)
        if component and not component.x_bas_ref:
            component.x_bas_ref = LABEL
            done.append(xmlid)
    _logger.info('KSW_commissions: BAS description «%s» set on %s.',
                 LABEL, ', '.join(done) or 'nothing')
