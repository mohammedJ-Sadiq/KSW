"""Client Account Number moves into the contact's Reference (`ref`).

The field becomes an alias of `ref`; its old column is copied first. A contact
whose Reference already holds something else stops the upgrade: guessing
which of the two is the BAS account would silently rematch his BAS history.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'res_partner' AND column_name = 'x_client_account_number'""")
    if not cr.fetchone():
        return
    _move_into_ref(cr, 'x_client_account_number')


def _move_into_ref(cr, column):
    cr.execute(f"""
        SELECT id, name, ref, {column} FROM res_partner
         WHERE NULLIF(btrim({column}), '') IS NOT NULL
           AND NULLIF(btrim(ref), '') IS NOT NULL
           AND btrim(ref) <> btrim({column})
    """)
    conflicts = cr.fetchall()
    if conflicts:
        raise Exception(
            'BAS account code: these contacts already have a different Reference. '
            'Clear one of the two, then upgrade again: ' + '; '.join(
                f'#{pid} {name!r}: Reference {ref!r}, BAS {code!r}'
                for pid, name, ref, code in conflicts[:30]))
    cr.execute(f"""
        UPDATE res_partner SET ref = btrim({column})
         WHERE NULLIF(btrim({column}), '') IS NOT NULL
           AND NULLIF(btrim(ref), '') IS NULL
    """)
    _logger.info('BAS account code: %s contacts got their %s as Reference.', cr.rowcount, column)
