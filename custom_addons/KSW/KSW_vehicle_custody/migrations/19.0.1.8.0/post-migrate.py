import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

# User decision 2026-11-25: a driver change in the Isuzu branches goes through
# HR and a workshop checkup, like a vehicle received from another department;
# everywhere else (trailers) the supervisor's confirmation completes it.
_BRANCHES = (("Tariq's Branch", 'فرع طارق'), ("Disouky's Branch", 'فرع الدسوقي'))


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Department = env['hr.department'].with_context(active_test=False)
    for english, arabic in _BRANCHES:
        department = Department.search([('x_fleet_holder', '=', True), '|',
                                        ('name', '=', english), ('name', '=', arabic)], limit=1)
        if not department:
            department = Department.with_context(lang='ar_001').search(
                [('x_fleet_holder', '=', True), ('name', '=', arabic)], limit=1)
        if department:
            department.x_driver_change_workshop = True
            _logger.info('Driver change through workshop: %s (%s)', department.name, department.id)
        else:
            _logger.warning('Driver change through workshop: no vehicle department named %s; '
                            'tick it under Vehicle Departments.', english)
    # Driver changes still with their supervisor follow the new setting.
    drafts = env['ksw.vehicle.custody.request'].search([('kind', '=', 'driver_change'), ('state', '=', 'draft')])
    drafts._stamp_route()
    _logger.info('Driver change drafts through workshop: %s of %s', len(drafts.filtered('through_workshop')), len(drafts))
