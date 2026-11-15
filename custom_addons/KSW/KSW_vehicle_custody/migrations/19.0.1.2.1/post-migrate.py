from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    # To-dos were also given to admin (he holds the workshop/HR roles for
    # system access). Re-sync every open form: admin's are removed.
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['ksw.vehicle.custody.request'].search(
        [('state', 'in', ('draft', 'hr', 'workshop', 'done'))])._sync_activities()
