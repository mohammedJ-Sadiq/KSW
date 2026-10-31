"""The contact's BAS Account Code moves into its Reference (`ref`), shared
with the customer sync's Client Account Number. See KSW_ext_sync 19.0.1.6.0."""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'res_partner' AND column_name = 'x_bas_code'""")
    if not cr.fetchone():
        return
    cr.execute("""
        SELECT id, name, ref, x_bas_code FROM res_partner
         WHERE NULLIF(btrim(x_bas_code), '') IS NOT NULL
           AND NULLIF(btrim(ref), '') IS NOT NULL
           AND btrim(ref) <> btrim(x_bas_code)
    """)
    conflicts = cr.fetchall()
    if conflicts:
        raise Exception(
            'BAS account code: these contacts already have a different Reference. '
            'Clear one of the two, then upgrade again: ' + '; '.join(
                f'#{pid} {name!r}: Reference {ref!r}, BAS {code!r}'
                for pid, name, ref, code in conflicts[:30]))
    cr.execute("""
        UPDATE res_partner SET ref = btrim(x_bas_code)
         WHERE NULLIF(btrim(x_bas_code), '') IS NOT NULL
           AND NULLIF(btrim(ref), '') IS NULL
    """)
    _logger.info('BAS account code: %s contacts got their BAS code as Reference.', cr.rowcount)
    # Two contacts for one BAS account (the sync and the import each made
    # one) must be merged by hand: Contacts > Action > Merge.
    cr.execute("""SELECT ref, array_agg(id ORDER BY id) FROM res_partner
                   WHERE active AND NULLIF(btrim(ref), '') IS NOT NULL
                   GROUP BY ref HAVING count(*) > 1""")
    for ref, ids in cr.fetchall():
        _logger.warning('BAS account %s is on several contacts %s: merge them.', ref, ids)
