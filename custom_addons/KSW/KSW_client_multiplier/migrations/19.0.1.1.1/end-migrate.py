"""Existing customers with no multiplier start at 1 (decided 2026-10-28).

end-migrate, not post: the BAS account number on a contact (`x_bas_code`)
belongs to KSW_bas_gl_import, which loads after this module, and a post script
would not see the field (KSW gotcha #45). See
ksw.client.multiplier._backfill_existing_customers for who is left out.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['ksw.client.multiplier']._backfill_existing_customers()
