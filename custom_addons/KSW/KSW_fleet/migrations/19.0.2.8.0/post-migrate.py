"""Plates typed before 19.0.2.8.0 were stored in whatever script was typed:
store both (Arabic in plate_number, English in x_plate_en)."""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    vehicles = env['ksw.fleet.vehicle'].with_context(active_test=False).search([('plate_number', '!=', False)])
    for vehicle in vehicles:
        vehicle.write({'plate_number': vehicle.plate_number})
