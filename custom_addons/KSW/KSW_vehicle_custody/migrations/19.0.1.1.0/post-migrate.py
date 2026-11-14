from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    # Until 19.0.1.1.0 HR could approve a release's hand-in forms one by one,
    # which moved the forms to the GM but left the request with HR, where the
    # GM never saw it (VA/2026/0512). Move such requests on.
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['ksw.fleet.request'].search([('state', '=', 'hr_review')])._ksw_custody_review_done()
