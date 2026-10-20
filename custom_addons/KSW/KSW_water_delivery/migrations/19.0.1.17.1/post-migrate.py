"""Water branches are known by name, not by their BAS code.

The operation types were created as "Water Delivery Notes" / "Water Delivery
Notes 120" and so on, so every picker showed the number. BAS has no branch
master for CODE2; these names come from the cash account each code posts its
cash sales to (cod10.DNAME, checked 2026-10-11).

Only a type still carrying the generated name is renamed, so a name somebody
has already typed is kept. Codes with no known name (135, 152, 160, 175) are
left for the user to name in Water Delivery > Configuration > Branches.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

BRANCH_NAMES = {
    '120': ('Nabiya', 'النابية'),
    '140': ('Jubail', 'الجبيل'),
    '145': ('Manifa', 'منيفة'),
    '170': ('Othmaniya', 'العثمانية'),
    '171': ('North Manifa', 'شمال منيفة'),
    '172': ('Scope Factory', 'مصنع سكوب'),
    '180': ('Qairawan', 'القيروان'),
    '185': ('Tabuk 2', 'تبوك 2'),
    '190': ('Haradh', 'حرض'),
}
GENERATED_PREFIX = 'Water Delivery Notes'


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    types = env['stock.picking.type'].with_context(active_test=False).search(
        [('x_is_water_delivery', '=', True)])
    for ptype in types:
        names = BRANCH_NAMES.get((ptype.x_branch_code or '').strip())
        current = ptype.with_context(lang='en_US').name or ''
        if not names or not current.startswith(GENERATED_PREFIX):
            continue
        english, arabic = names
        ptype.with_context(lang='en_US').name = english
        ptype.update_field_translations('name', {'ar_001': arabic})
        _logger.info('Water branch %s renamed %r -> %r', ptype.x_branch_code,
                     current, english)
