import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Stamp the new Booleans FALSE on every existing water note.

    A Boolean column added by an upgrade holds NULL, not FALSE (KSW gotcha
    #146). NULL reads as False in Python, so nothing looks wrong -- but
    `('x_issued_offline', '=', False)` does not match a NULL row in SQL, so
    every note issued before today would silently drop out of the filters that
    are supposed to show the whole population. That is exactly the failure the
    `x_signature_required` migration was written for one version ago.
    """
    cr.execute("""
        UPDATE stock_picking
           SET x_issued_offline = COALESCE(x_issued_offline, FALSE),
               x_location_exception = COALESCE(x_location_exception, FALSE)
         WHERE x_is_water_delivery = TRUE
           AND (x_issued_offline IS NULL OR x_location_exception IS NULL)
    """)
    _logger.info(
        'KSW_water_delivery: stamped %s existing water notes as issued online '
        'and location-confirmed.', cr.rowcount,
    )
