"""Rebuild the payment mirror: its key was missing SERIAL.

`ksw.bas.payment` mirrors VOU10 *journal lines*, whose real primary key is
FTYPE + FTYPE2 + CODE2 + NUMBER1 + **SERIAL** (BAS_DATABASE_REFERENCE.md).
`_make_key` left SERIAL out, so one receipt with 172 lines produced 172 rows
sharing a `bas_key`. Harmless while the sync never completed and every run
rolled back; the moment it completed (2026-09-02), the keyed upsert began
resolving each duplicate to one arbitrary record and overwriting it with
another line's amount and account.

Every row is a mirror of BAS and is rebuilt from it, so the repair is simply
to drop them and clear the watermark: the next cron run re-reads the full
fiscal year and writes correctly-keyed lines. Nothing here is user data.

Touches only this module's own model, so post-migrate is the right stage
(a field from a later module would need end-migrate — pitfall #104).
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute("SELECT count(*) FROM ksw_bas_payment")
    before = cr.fetchone()[0]

    cr.execute("""
        SELECT count(*) FROM (
            SELECT bas_key FROM ksw_bas_payment
            GROUP BY bas_key HAVING count(*) > 1
        ) t
    """)
    dup_keys = cr.fetchone()[0]

    cr.execute("DELETE FROM ksw_bas_payment")
    cr.execute(
        "DELETE FROM ir_config_parameter WHERE key = 'ksw_bas.last_sync_payment'")

    _logger.info(
        'KSW BAS: payment mirror cleared for rebuild — %s rows dropped '
        '(%s duplicate voucher keys); watermark reset so the next sync '
        're-reads the full fiscal year with SERIAL in the key.',
        before, dup_keys)
