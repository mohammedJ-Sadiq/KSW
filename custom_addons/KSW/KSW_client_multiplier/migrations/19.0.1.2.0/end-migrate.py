"""Every customer contact gets its own client (Oct 2026).

BAS clients take the contact they belong to (mirror link, else the account
number on the contact); customers left without one start at 1. end-migrate:
`x_bas_code` is KSW_bas_gl_import's, loaded after this module (gotcha #45).
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Client = env['ksw.client.multiplier']
    Client._link_contacts()
    Client._backfill_existing_customers()
    # The client is now named after its contact; stored names predate that.
    env.add_to_compute(Client._fields['name'], Client.search([]))
    Client.flush_model(['name'])
