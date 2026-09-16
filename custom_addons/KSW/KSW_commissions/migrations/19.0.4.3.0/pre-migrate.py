"""Remap ``ksw.site.site_type`` before the new selection is loaded.

Only odoo_dev is affected: the field shipped there earlier the same day
with the values ``internal`` / ``external``, which the released selection
(``location`` / ``calculation``) does not contain. Left alone, those rows
would read back as an invalid selection value for the length of the
upgrade. Every one of them is an overtime location — the trip side no
longer names a site at all — so both collapse to ``location``.

Guarded on the column existing, because in KSWCO it does not yet.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute("""
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'ksw_site' AND column_name = 'site_type'
    """)
    if not cr.fetchone():
        return

    cr.execute("""
        UPDATE ksw_site SET site_type = 'location'
         WHERE site_type IN ('internal', 'external')
    """)
    if cr.rowcount:
        _logger.info(
            'KSW_commissions: %s work site(s) remapped to the released '
            'site_type values.', cr.rowcount)
