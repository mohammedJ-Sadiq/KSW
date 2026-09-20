import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Land drivers straight on their own app.

    `res.users.action_id` is Odoo's Home Action: what the user sees on login and
    when the web client has nowhere else to go. For a driver that should be My
    Deliveries, not the apps home screen — he has ten apps visible (the other
    KSW modules push their baseline groups onto every internal user) and none of
    them are his job.

    Only for the driver tier: a dispatcher or a billing clerk holding the same
    group still works across the rest of Odoo, so sending them to a kanban of
    deliveries would be wrong.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})

    action = env.ref(
        'KSW_water_delivery.action_water_delivery_my_notes', raise_if_not_found=False)
    if not action:
        _logger.warning('KSW_water_delivery: My Deliveries action is missing; '
                        'home action not set.')
        return

    driver = env.ref('KSW_water_delivery.group_water_driver')
    dispatcher = env.ref('KSW_water_delivery.group_water_dispatcher')
    billing = env.ref('KSW_water_delivery.group_water_billing')

    # NOT a single domain with `not in`: res.users._search_all_group_ids passes
    # the operator straight onto a to-many path
    # (`[('group_ids.all_implied_ids', operator, value)]`), and on a to-many
    # `not in` means "has SOME group that is not billing" rather than "has no
    # billing group" -- so every driver matches and nobody is excluded.
    # Search the positives and subtract. (Cousin of KSW gotcha #24.)
    Users = env['res.users'].with_context(active_test=False)
    drivers = Users.search([('all_group_ids', 'in', driver.ids)])
    drivers -= Users.search([('all_group_ids', 'in', (dispatcher | billing).ids)])
    if not drivers:
        _logger.info('KSW_water_delivery: no driver-only users to set a home action for.')
        return

    # Never overwrite a home action somebody chose deliberately.
    untouched = drivers.filtered(lambda u: not u.action_id)
    untouched.write({'action_id': action.id})
    _logger.info(
        'KSW_water_delivery: home action set to "My Deliveries" for %s driver(s); '
        '%s left alone (already had one).',
        len(untouched), len(drivers) - len(untouched),
    )
