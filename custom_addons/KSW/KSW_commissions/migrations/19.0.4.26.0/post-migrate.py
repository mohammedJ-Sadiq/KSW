"""Sub-batches belong to one component batch now (batch_id).

A sub-batch whose rows all sit in one batch gets that batch. One whose rows
span several components (made under the old cross-component rule), or that
has no rows, is left without one and keeps drawing from the whole month —
its contents do not move.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        UPDATE ksw_pay_sub_batch s
           SET batch_id = one.batch_id
          FROM (SELECT x_sub_batch_id AS sub_id, MIN(batch_id) AS batch_id
                  FROM ksw_pay_entry
                 WHERE x_sub_batch_id IS NOT NULL
              GROUP BY x_sub_batch_id
                HAVING COUNT(DISTINCT batch_id) = 1) one
         WHERE one.sub_id = s.id AND s.batch_id IS NULL
    """)
    _logger.info('KSW_commissions: %s sub-batch(es) tied to their component '
                 'batch.', cr.rowcount)
    cr.execute("""
        UPDATE ksw_pay_sub_batch s
           SET component_id = b.component_id
          FROM ksw_pay_batch b
         WHERE b.id = s.batch_id AND s.component_id IS NULL
    """)
