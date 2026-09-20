import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Undo the home action given to users who are not driver-only.

    19.0.1.4.0 selected them with a single domain ending in
    `('all_group_ids', 'not in', ...)`. That does not exclude anybody:
    `res.users._search_all_group_ids` forwards the operator onto a to-many path,
    where `not in` is satisfied by having SOME group outside the list rather
    than by having none inside it. A dispatcher or billing clerk who also holds
    the driver group was therefore sent to My Deliveries on login, cut off from
    the rest of Odoo he needs.

    Only clears the home action where it points at OUR action -- a home action
    somebody set for themselves is none of this script's business.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})

    action = env.ref(
        'KSW_water_delivery.action_water_delivery_my_notes', raise_if_not_found=False)
    if not action:
        return

    driver = env.ref('KSW_water_delivery.group_water_driver')
    dispatcher = env.ref('KSW_water_delivery.group_water_dispatcher')
    billing = env.ref('KSW_water_delivery.group_water_billing')

    Users = env['res.users'].with_context(active_test=False)
    drivers = Users.search([('all_group_ids', 'in', driver.ids)])
    wider = Users.search([('all_group_ids', 'in', (dispatcher | billing).ids)])

    # Compare by id, not by record: `res.users.action_id` is a Many2one to
    # `ir.actions.actions` while env.ref() hands back an `ir.actions.act_window`.
    # Same row, different model, so `==` is always False and the guard silently
    # matches nobody.
    wrongly_set = (drivers & wider).filtered(lambda u: u.action_id.id == action.id)
    if wrongly_set:
        wrongly_set.write({'action_id': False})
        _logger.info(
            'KSW_water_delivery: cleared the driver home action from %s user(s) '
            'who also hold a dispatcher or billing role: %s',
            len(wrongly_set), ', '.join(wrongly_set.mapped('login')),
        )

    driver_only = (drivers - wider).filtered(lambda u: not u.action_id)
    if driver_only:
        driver_only.write({'action_id': action.id})
        _logger.info('KSW_water_delivery: home action set for %s driver-only user(s).',
                     len(driver_only))
