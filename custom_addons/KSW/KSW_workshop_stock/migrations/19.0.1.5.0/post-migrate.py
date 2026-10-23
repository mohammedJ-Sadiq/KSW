# Workshop spare parts are bought and issued to repairs, never sold, so they
# must not appear in Accounting > Customers > Products next to the BAS items.
# The catalogue is noupdate, so the data file alone does not reach existing
# databases.
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    ids = env['ir.model.data'].search([
        ('module', '=', 'KSW_workshop_stock'),
        ('model', '=', 'product.template'),
    ]).mapped('res_id')
    env['product.template'].with_context(active_test=False).browse(ids).exists() \
        .filtered('sale_ok').write({'sale_ok': False})
