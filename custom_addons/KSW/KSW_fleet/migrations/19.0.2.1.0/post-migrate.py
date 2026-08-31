"""19.0.2.1.0 — give every company's own partner the customer role.

``client_id`` on ksw.fleet.vehicle (and on ksw.workshop.request) now restricts
its picker to ``[('customer_rank', '>', 0)]`` so employees and other non-client
contacts stop appearing in it. Both fields default to ``env.company.partner_id``
— the company's own fleet is just one client among others — and that partner
normally carries ``customer_rank = 0``, which would put the default value
outside its own domain on every new record, and put all 371 existing vehicles /
17,080 existing workshop requests outside it too.

Stamping the company partner as a customer is the honest fix: the workshop
genuinely treats KSW's own fleet as a client, which is how this was modelled
in 19.0.2.0.0 in the first place.

Idempotent, and keyed on the value actually being changed (``customer_rank``
still 0) rather than on a column ``_auto_init`` may already have backfilled —
the trap that made the 19.0.2.0.0 migration above silently skip its work.

Shares its implementation with ``_post_init_hook`` in the module's __init__ so
the install path and the upgrade path cannot drift.
"""
import logging

from odoo import api, SUPERUSER_ID

from odoo.addons.KSW_fleet import _mark_company_partners_as_customers

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})
    before = env['res.company'].sudo().search([]).partner_id.filtered(
        lambda p: not p.customer_rank)
    if not before:
        _logger.info(
            "KSW_fleet 19.0.2.1.0: every company partner already carries the "
            "customer role — nothing to do.")
        return

    names = ', '.join(before.mapped('name'))
    _mark_company_partners_as_customers(env)
    _logger.info(
        "KSW_fleet 19.0.2.1.0: stamped customer_rank=1 on %s company "
        "partner(s): %s", len(before), names)
