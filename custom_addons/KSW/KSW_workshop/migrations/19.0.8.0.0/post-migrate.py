"""19.0.8.0.0 — seed the workshop client registry.

`ksw.workshop.request.client_id` (and `ksw.fleet.vehicle.client_id`, from this
module) narrowed from "any partner with a customer role" to "a partner
registered with the workshop". `customer_rank > 0` stopped being a useful
filter when the BAS customer sync landed: 819 partners carry it on odoo_dev
and exactly one of them owns a vehicle.

Narrowing a domain strands every existing row that falls outside the new one,
so this registers every partner the workshop already points at — the 17,080
requests, the 371 vehicles, and every company partner (which is also
`client_id`'s default value, and would otherwise sit outside its own field's
domain on a brand-new form).

The registration is done by `_register_existing_clients`, the same function
the post-install hook calls, so a fresh install and an upgrade cannot drift.
It is idempotent — archived registrations count as known, so re-running it
never resurrects a client the manager retired.

post-migrate, not end-migrate: it touches only this module's model and
KSW_fleet's, both loaded by the time it runs.
"""

import logging

from odoo import SUPERUSER_ID, api

from odoo.addons.KSW_workshop.models.ksw_workshop_client import _register_existing_clients

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    added = _register_existing_clients(env)
    total = env['ksw.workshop.client'].with_context(active_test=False).search_count([])
    _logger.info(
        'KSW_workshop: registered %s workshop client(s); %s registration(s) now on file.',
        added, total,
    )
