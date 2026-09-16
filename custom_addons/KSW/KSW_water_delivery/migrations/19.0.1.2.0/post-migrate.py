import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

# A trailer carries 32 m³. Seeded only as a starting point: the figure lives on
# each vehicle from here on, because an Isuzu trip is not a trailer trip.
_TRAILER_CAPACITY_M3 = 32.0


def migrate(cr, version):
    """Retire the module's own placeholder product, and seed trip volumes.

    The module originally seeded a product of its own, "Sweet Water — Load",
    because nothing was known about what KSW sells. It turns out
    KSW_bas_gl_import had already created every BAS item as an Odoo product,
    correctly named and already measured in m³ — so the placeholder was a
    duplicate of records that were already right. The rate register now decides
    which products a driver may pick, and it is keyed on the real ones.

    Archived rather than deleted: notes already issued reference it, and a
    product behind a validated stock move cannot go away (KSW practice: archive,
    never delete, when a foreign key is real).
    """
    env = api.Environment(cr, SUPERUSER_ID, {})

    placeholder = env.ref(
        'KSW_water_delivery.product_water_load', raise_if_not_found=False,
    )
    if placeholder and placeholder.active:
        placeholder.active = False
        _logger.info(
            'KSW_water_delivery: archived the placeholder product %s — the real '
            'BAS-mapped products are used instead.', placeholder.display_name,
        )

    trailers = env['ksw.fleet.vehicle'].search([
        ('vehicle_type', '=', 'trailer'), ('x_capacity_m3', '=', 0.0),
    ])
    if trailers:
        trailers.write({'x_capacity_m3': _TRAILER_CAPACITY_M3})
        _logger.info(
            'KSW_water_delivery: seeded %s trailers with a %s m³ trip volume. '
            'Other vehicle types are left empty on purpose — an Isuzu trip is '
            'not a trailer trip, and a wrong number bills the client wrongly.',
            len(trailers), _TRAILER_CAPACITY_M3,
        )

    # Existing notes predate the quantity/location columns. Stamp what was true
    # when they were issued rather than leaving NULL to be read as a value
    # (KSW gotcha #146).
    cr.execute("""
        UPDATE stock_picking sp
           SET x_entered_qty = COALESCE((
                   SELECT SUM(sm.quantity) FROM stock_move sm
                    WHERE sm.picking_id = sp.id), 0),
               x_entered_uom = 'product'
         WHERE sp.x_is_water_delivery = TRUE AND sp.x_entered_uom IS NULL
    """)
    _logger.info('KSW_water_delivery: %s existing notes stamped with their '
                 'entered quantity.', cr.rowcount)
