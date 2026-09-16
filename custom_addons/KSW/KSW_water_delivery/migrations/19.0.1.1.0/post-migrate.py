import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Start the pilot branch without signatures, for the proof of concept.

    Drivers issue the note at the customer to prove they can; the paper note
    still collects the wet signature. The signing half of the module is built
    and tested -- ticking "Require a Signature" on the operation type turns it
    on, and nothing else changes.

    Two halves, and the second is the one that matters:

    1) The operation type is seeded inside a `noupdate="1"` block, so editing
       the data file would never reach the record that already exists (KSW
       gotcha #2). It has to be written here.

    2) Every water note that ALREADY exists was issued under the old rule,
       where a signature was required. Stamp them TRUE explicitly -- a Boolean
       column added later holds NULL, not FALSE (gotcha #146), and NULL would
       both drop them from `WHERE x_signature_required` in SQL and read as
       False in Python, silently reclassifying signed history as
       proof-of-concept data.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})

    cr.execute("""
        UPDATE stock_picking
           SET x_signature_required = TRUE
         WHERE x_is_water_delivery = TRUE
           AND x_signature_required IS NULL
    """)
    _logger.info(
        'KSW_water_delivery: %s existing water delivery notes stamped as '
        'having required a signature.', cr.rowcount,
    )

    picking_type = env.ref(
        'KSW_water_delivery.picking_type_water_out', raise_if_not_found=False,
    )
    if not picking_type:
        _logger.warning(
            'KSW_water_delivery: the pilot operation type is gone; set '
            '"Require a Signature" by hand on whichever type replaced it.'
        )
        return

    picking_type.x_signature_required = False
    _logger.info(
        'KSW_water_delivery: %s now issues notes WITHOUT a signature '
        '(proof of concept). Tick "Require a Signature" on the operation type '
        'to turn signing on.', picking_type.display_name,
    )
