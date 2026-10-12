import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Send driver-only users' Home Action to the driver app.

    The driver app (/water/) is now the only place a driver issues a note:
    online it sends at once, offline it queues. A driver whose Home Action
    still opens My Deliveries or the old New Delivery form would land in the
    web client on login, the screen this release retires.

    Only rewrites a Home Action that points at one of OUR two old actions; one
    somebody chose for themselves is left alone. Compared by id, because
    `res.users.action_id` is an `ir.actions.actions` and env.ref() returns the
    concrete model (Pitfalls #153).
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    app = env.ref('KSW_water_delivery.action_water_driver_app', raise_if_not_found=False)
    if not app:
        return
    old_ids = {
        action.id for action in (
            env.ref('KSW_water_delivery.action_water_delivery_my_notes', raise_if_not_found=False),
            env.ref('KSW_water_delivery.action_water_delivery_wizard', raise_if_not_found=False),
        ) if action
    }
    driver = env.ref('KSW_water_delivery.group_water_driver')
    dispatcher = env.ref('KSW_water_delivery.group_water_dispatcher')
    billing = env.ref('KSW_water_delivery.group_water_billing')

    Users = env['res.users'].with_context(active_test=False)
    drivers = Users.search([('all_group_ids', 'in', driver.ids)])
    wider = Users.search([('all_group_ids', 'in', (dispatcher | billing).ids)])
    moved = (drivers - wider).filtered(
        lambda u: not u.action_id or u.action_id.id in old_ids)
    if moved:
        moved.write({'action_id': app.id})
        _logger.info('KSW_water_delivery: %s driver(s) now open the driver app on login: %s',
                     len(moved), ', '.join(moved.mapped('login')))
