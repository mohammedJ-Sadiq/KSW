"""19.0.2.0.0 — backfill client_id / vehicle_type on existing requests.

Every ksw.workshop.request already has a vehicle_id, and by the time this
runs KSW_fleet's own 19.0.2.0.0 migration has already populated that
vehicle's client_id/vehicle_type (KSW_workshop depends on KSW_fleet, so its
migration runs first whenever both are upgraded together). This is a pure
derivation from already-migrated data — no guessing needed here.

Raw SQL, not a per-record ORM write() loop: there are 17,000+ historical
requests, and both client_id and vehicle_type carry tracking=True, so an ORM
write() loop would be slow AND spam every request's chatter with a "changed
Client from Nothing to ..." tracking message. A single UPDATE avoids both.

Keys off vehicle_type IS NULL, not client_id IS NULL: client_id has a Python
default (env.company.partner_id) and Odoo's _auto_init backfills every
existing row with a new column's default BEFORE any post-migrate script
runs — by the time this runs, client_id is already set on every row, so
filtering on it would match nothing. vehicle_type has no default, so it's
the reliable "not yet migrated" signal (same trap, same fix, as KSW_fleet's
own post-migrate.py).
"""


def migrate(cr, version):
    if not version:
        return

    cr.execute("""
        UPDATE ksw_workshop_request req
        SET client_id = veh.client_id, vehicle_type = veh.vehicle_type
        FROM ksw_fleet_vehicle veh
        WHERE req.vehicle_id = veh.id
          AND req.vehicle_type IS NULL
          AND req.vehicle_id IS NOT NULL
    """)
