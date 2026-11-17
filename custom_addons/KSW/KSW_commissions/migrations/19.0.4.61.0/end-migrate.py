"""Commission-only settlement (Nov 2026): the GM's Home counter.

KSW_backend_theme's sources file is noupdate, so an existing database gets
the counter here, under that module's xml id, so a later upgrade of the
theme does not add it twice. end-migrate: ksw.home.inbox.source belongs to
KSW_backend_theme, which loads after this module (Odoo 19 Pitfalls #45).
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

XMLID = 'inbox_deduction_commission_only'
VALS = {
    'name': 'Commission-only settlements awaiting my decision',
    'sequence': 21,
    'model_name': 'ksw.deduction.commission.only',
    'domain': "[('x_is_pending_my_action', '=', True)]",
    'icon': 'fa-exchange',
}
NAME_AR = 'طلبات تسوية الخصم من العمولة فقط بانتظار قراري'


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    if 'ksw.home.inbox.source' not in env:
        _logger.info('KSW_backend_theme not installed: no Home counter.')
        return
    record = env.ref('KSW_backend_theme.%s' % XMLID, raise_if_not_found=False)
    if not record:
        record = env['ksw.home.inbox.source'].create(VALS)
        env['ir.model.data'].create({
            'module': 'KSW_backend_theme', 'name': XMLID,
            'model': record._name, 'res_id': record.id, 'noupdate': True})
        _logger.info('Home counter %s created (id %s).', XMLID, record.id)
    if env['res.lang']._lang_get('ar_001'):
        record.with_context(lang='ar_001').write({'name': NAME_AR})
