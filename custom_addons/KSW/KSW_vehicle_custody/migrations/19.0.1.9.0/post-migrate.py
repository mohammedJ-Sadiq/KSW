import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

# Oil and filters changed at every service: left out of the HR-12 maintenance
# history. Named by what the item IS (it starts with the word), so an oil pump,
# oil cooler or filter base stays in. The workshop manager adjusts the tick.
_ROUTINE = r'^\s*(فلتر|فلاتر|طقم فلاتر|زيت)(\s|$)'


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    cr.execute("""SELECT id FROM ksw_workshop_part
                   WHERE COALESCE(NULLIF(description, ''), name) ~ %s""", (_ROUTINE,))
    parts = env['ksw.workshop.part'].with_context(active_test=False).browse([r[0] for r in cr.fetchall()])
    parts.write({'x_routine_service': True})
    _logger.info('Routine service items ticked: %s (%s)', len(parts), ', '.join(parts.mapped('display_name')))
    # Forms the workshop has not inspected yet start from the last reading.
    forms = env['ksw.vehicle.custody.request'].search([('state', 'in', ('draft', 'hr', 'workshop')),
                                                       ('workshop_done', '=', False)])
    forms._stamp_inspection_baseline()
    _logger.info('HR-12 forms given their last inspection: %s of %s',
                 len(forms.filtered('baseline_request_id')), len(forms))
