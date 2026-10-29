"""Branches and vehicles become top-level analytic plans (Oct 2026).

So a line can carry a branch, a vehicle and an employee at once (one column
per root plan). Odoo moves the existing analytic-line tags into the new
columns itself; see ksw.bas.gl.import._ksw_split_cost_centre_plans. A no-op on
a database without the BAS cost centres.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    roots = env['ksw.bas.gl.import']._ksw_split_cost_centre_plans()
    _logger.info('KSW_bas_gl_import: cost-centre plans made top-level: %s',
                 ', '.join(p.name for p in roots.values()) or 'none (no BAS cost centres)')
