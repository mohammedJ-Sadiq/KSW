import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Every water branch onto the strict rule (Oct 2026).

    Decided with the user: no delivery note for a client without an approved
    location, anywhere. The rule lives on each operation type, and the pilot's
    is a `noupdate` data record, so changing the field default reaches nothing
    that already exists (KSW gotcha #2) -- hence this.

    Safe on KSWCO at the time of writing: the pilot branch had issued 0 notes
    and located 0 of its 58 clients, so nothing in flight is stopped.
    """
    cr.execute("""
        UPDATE stock_picking_type
           SET x_location_rule = 'enforce'
         WHERE x_is_water_delivery = TRUE
           AND x_location_rule IS DISTINCT FROM 'enforce'
    """)
    _logger.info(
        'KSW_water_delivery: %s water operation type(s) switched to the strict '
        'location rule.', cr.rowcount,
    )
