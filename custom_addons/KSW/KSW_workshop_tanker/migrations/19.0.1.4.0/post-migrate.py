"""Rename the tedders imported as TK068 to «تيدر 68» (2026-10-14).

They were first named as "tanker trailers" with a Latin TK code; within hours
requesters were picking TK137 for trailer 137. They go back to the name the
workshop and BAS use. Raw SQL: the name is not tracked, and nothing derives
from it.
"""
import logging
import re

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("SELECT id, name FROM ksw_fleet_vehicle WHERE vehicle_type = 'tank' AND name ~ '^TK[0-9]+$'")
    rows = cr.fetchall()
    for vehicle_id, name in rows:
        number = int(re.sub(r'\D', '', name))
        cr.execute("UPDATE ksw_fleet_vehicle SET name = %s WHERE id = %s", (f'تيدر {number}', vehicle_id))
    _logger.info('Renamed %s tedders from TKnnn to «تيدر n»', len(rows))
