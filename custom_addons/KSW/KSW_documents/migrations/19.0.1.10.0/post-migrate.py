from odoo import SUPERUSER_ID, api

# What a vehicle handover checks, set once: the types are noupdate (HR tunes
# them), so a <function> in the data file would undo HR's choice on every
# upgrade. The driver card stays off until the register holds the cards
# (none on 2026-11-23: switching it on would stop every driver change).
CHECKED = ('istimara', 'insurance', 'mvpi', 'operation_card', 'water_tank_licence', 'driving_licence', 'iqama')


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['ksw.document.type'].search([('code', 'in', CHECKED)]).write({'handover_check': True})
