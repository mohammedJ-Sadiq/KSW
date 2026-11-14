import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """19.0.1.2.0: a vehicle request is an intent, decided by the GM alone.
    HR's step before him is gone, and HR-12 forms open when he decides."""
    # Requests left with HR go to the GM.
    cr.execute("UPDATE ksw_fleet_request SET state = 'submitted' WHERE state = 'hr_review'")
    _logger.info('Vehicle requests moved from HR Review to the GM: %s', cr.rowcount)
    # Hand-in forms opened before the GM decided: cancelled, unlinked; his
    # decision opens a new one.
    cr.execute("""
        UPDATE ksw_vehicle_custody_request SET state = 'cancelled'
         WHERE kind = 'hand_in' AND state IN ('hr', 'gm') AND workshop_request_id IS NULL
        RETURNING id
    """)
    cancelled = [r[0] for r in cr.fetchall()]
    if cancelled:
        cr.execute("UPDATE ksw_fleet_release_line SET x_custody_request_id = NULL "
                   "WHERE x_custody_request_id = ANY(%s)", [cancelled])
    _logger.info('Hand-in forms opened before the GM decided, cancelled: %s', len(cancelled))
    # Forms already past HR keep that: HR's part is done.
    cr.execute("""
        UPDATE ksw_vehicle_custody_request SET hr_done = TRUE
         WHERE state IN ('workshop', 'done', 'signed') AND hr_checked_by_id IS NOT NULL
    """)
    cr.execute("UPDATE ksw_vehicle_custody_request SET workshop_done = TRUE "
               "WHERE state IN ('done', 'signed')")
