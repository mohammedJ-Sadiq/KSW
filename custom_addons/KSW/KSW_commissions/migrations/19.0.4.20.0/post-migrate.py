""""Other" and every bonus are «بدل عمل اضافي» on the BAS voucher.

The accountant's wording: «غير ذلك» and «مكافأة …» are not descriptions he
posts under. All six already post to 3201010006 / 2107010001, so with the
same description the journal now carries them as one line per employee
(``_bas_detail_lines``) and the Excel summary as one column.

Only where the field is still empty, so a value typed on the component is
never overwritten; archived components included, as in 19.0.4.17.0.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

LABEL = 'بدل عمل اضافي'
XMLIDS = (
    'pay_component_other',
    'pay_component_bonus_foundation',
    'pay_component_bonus_national',
    'pay_component_bonus_fitr',
    'pay_component_bonus_adha',
    'pay_component_employee_bonus',
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
