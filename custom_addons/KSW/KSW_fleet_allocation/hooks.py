import logging

_logger = logging.getLogger(__name__)

# The departments that held vehicles when the module was introduced
# (Nov 2026). Matched by English name; any not found is logged, and the
# administrator ticks it under Configuration ▸ Vehicle Departments.
HOLDER_DEPARTMENTS = [
    'Supply Chain',
    'Supply Chain - Dammam',    # renamed Nov 2026 (restructure_departments.py)
    'Tabuk',
    'Supply Chain - Tabuk',     # renamed Nov 2026 (restructure_departments.py)
    "Disouky's Branch",
    "Tariq's Branch",
    'Equipment Rental Department',
]


def _post_init_hook(env):
    Department = env['hr.department'].with_context(lang='en_US')
    found = Department.search([('name', 'in', HOLDER_DEPARTMENTS)])
    found.write({'x_fleet_holder': True})
    missing = set(HOLDER_DEPARTMENTS) - set(found.mapped('name'))
    if missing:
        _logger.warning('Vehicle allocation: departments not found, flag them by hand: %s',
                        ', '.join(sorted(missing)))
    env['ksw.fleet.vehicle']._ksw_fleet_seed_from_drivers()
