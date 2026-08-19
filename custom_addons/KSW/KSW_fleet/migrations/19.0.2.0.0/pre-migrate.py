"""19.0.2.0.0 — log any non-empty 'vehicle_model' before the column goes inert.

vehicle_model conflated vehicle type and model into one free-text field; it's
being retired in favour of the new vehicle_type (Selection) + model (Char).
The 371 existing values are mostly blank or contain misparsed driver names
from the history-import feature (KSW_workshop's import_history.py), not real
model data, so they are deliberately NOT auto-migrated into the new 'model'
field — that would poison it from day one. This is just a safety-net log so
nothing is silently lost even though nothing is mapped automatically.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'ksw_fleet_vehicle' AND column_name = 'vehicle_model'"
    )
    if not cr.fetchone():
        return

    cr.execute(
        "SELECT name, vehicle_model FROM ksw_fleet_vehicle "
        "WHERE vehicle_model IS NOT NULL AND vehicle_model != ''"
    )
    rows = cr.fetchall()
    if rows:
        _logger.warning(
            "KSW_fleet 19.0.2.0.0: 'vehicle_model' is being retired; the column "
            "is left in place but unmapped. %s non-empty value(s) will no longer "
            "be shown anywhere. Logged here for manual review, not migrated "
            "(source data was not reliable enough to auto-map into 'model'): %s",
            len(rows), rows,
        )
