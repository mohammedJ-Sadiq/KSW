"""Sub-batches gained the days they cover (date_from / date_to).

Every sub-batch made before took its employees' rows for the whole month,
so that is the range it is given: its contents stay exactly as they are.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        UPDATE ksw_pay_sub_batch
           SET date_from = date_trunc('month', period)::date,
               date_to = (date_trunc('month', period)
                          + interval '1 month - 1 day')::date
         WHERE period IS NOT NULL
           AND (date_from IS NULL OR date_to IS NULL)
    """)
    _logger.info('KSW_commissions: %s sub-batch(es) given the whole month '
                 'as their days.', cr.rowcount)
