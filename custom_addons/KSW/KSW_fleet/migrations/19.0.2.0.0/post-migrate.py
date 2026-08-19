"""19.0.2.0.0 — backfill client_id / vehicle_type / state on existing vehicles.

Before this version every ksw.fleet.vehicle implicitly belonged to KSW's own
fleet — there was no client concept. client_id is now required, so the 371
vehicles already in the database (232 seeded from the live form + ~139
discovered during the KSW_workshop history import) need a value:

- client_id: the company's own partner (env.company.partner_id) for all of
  them — that is exactly what they represented before this change.
- vehicle_type: best-effort guess from the fleet-number prefix (IS -> Isuzu,
  T -> Trailer, else -> Other). Not guaranteed correct; the workshop manager
  can fix individual mis-guesses afterwards. This is the one place a guess is
  made, and only because there's a real signal (the prefix) to guess from.
- state: 'confirmed' — these are established, real vehicles, not new drafts
  awaiting review.

Idempotent: only touches rows where vehicle_type is still unset. Note this
keys off vehicle_type, NOT client_id — client_id has a Python-level default
(env.company.partner_id), and Odoo's _auto_init backfills every existing row
with a new column's default BEFORE any post-migrate script runs. By the time
this runs, client_id is already set on all 371 rows; searching on it would
find nothing and silently skip the whole vehicle_type guess. vehicle_type has
no default, so it's the reliable "not yet migrated" signal.
"""
import logging
from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})
    vehicles = env['ksw.fleet.vehicle'].search([('vehicle_type', '=', False)])
    if not vehicles:
        return

    is_vehicles = vehicles.filtered(lambda v: (v.name or '').upper().startswith('IS'))
    t_vehicles = (vehicles - is_vehicles).filtered(lambda v: (v.name or '').upper().startswith('T'))
    other_vehicles = vehicles - is_vehicles - t_vehicles

    is_vehicles.write({'vehicle_type': 'isuzu'})
    t_vehicles.write({'vehicle_type': 'trailer'})
    other_vehicles.write({'vehicle_type': 'other'})
    vehicles.write({'client_id': env.company.partner_id.id, 'state': 'confirmed'})

    _logger.info(
        "KSW_fleet 19.0.2.0.0: backfilled client_id + vehicle_type for %s vehicles "
        "(%s Isuzu, %s Trailer, %s Other).",
        len(vehicles), len(is_vehicles), len(t_vehicles), len(other_vehicles),
    )
