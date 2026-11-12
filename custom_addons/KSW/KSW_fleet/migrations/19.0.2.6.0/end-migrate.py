from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    # Custodian (19.0.2.6.0): seed it from the BAS driver, else the default
    # driver. end-migrate, not post: x_bas_driver_id belongs to
    # KSW_workshop_tanker, which loads after this module (gotcha #45).
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['ksw.fleet.vehicle']._ksw_seed_custodians()
