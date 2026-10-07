"""Tell each holiday bonus which day it pays for, and Friday Work Allowance
that it is a count of Fridays.

Until now every one of them was undated, so the vacation hold had nothing to
compare in the month somebody came back: KSWCO PB000081 flagged MD ANOWAR
HOSSAIN on the September National Day Bonus although he was back on 10
September and National Day was the 23rd.

Writing x_paid_day through the ORM dates the open rows from Public Holidays
(ksw.pay.component.write -> ksw.pay.entry._apply_paid_day, strict=False); a
month the calendar has no holiday for is left as it was. The holidays
themselves are tagged by the stored compute on resource.calendar.leaves,
from their names, when the column is created.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

PAID_DAY_BY_CODE = {
    'BONUS_NATIONAL': 'national_day',
    'BONUS_FOUNDATION': 'foundation_day',
    'BONUS_FITR': 'eid_fitr',
    'BONUS_ADHA': 'eid_adha',
    'FRIDAY': 'friday',
}


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    tagged = env['resource.calendar.leaves'].search(
        [('x_pay_occasion', '!=', False)])
    _logger.info('Public holidays tagged with a pay occasion: %s',
                 ', '.join('%s=%s' % (h.name, h.x_pay_occasion)
                           for h in tagged) or 'none')
    Component = env['ksw.pay.component'].with_context(active_test=False)
    for code, paid_day in PAID_DAY_BY_CODE.items():
        component = Component.search(
            [('code', '=', code), ('x_paid_day', '=', False)], limit=1)
        if component:
            component.write({'x_paid_day': paid_day})
            _logger.info('%s now paid for %s', code, paid_day)
