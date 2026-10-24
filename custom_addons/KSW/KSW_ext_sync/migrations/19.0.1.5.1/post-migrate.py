from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """The BAS app became Accounting > BAS Mirror; relabel it in Arabic too.

    The .po carries the new label, but a translation load never overwrites a
    value already in the database, so the old "مزامنة BAS" would stay.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    if not env['res.lang']._lang_get('ar_001'):
        return
    env.ref('KSW_ext_sync.menu_bas_root').with_context(lang='ar_001').write(
        {'name': 'بيانات BAS'})
