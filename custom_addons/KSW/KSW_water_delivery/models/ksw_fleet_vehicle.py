from odoo import fields, models


class KswFleetVehicle(models.Model):
    _inherit = 'ksw.fleet.vehicle'

    # Which branch's note numbering this tanker writes into. The branch follows
    # the truck rather than the driver: a driver can be lent to another branch
    # for a day, the tanker belongs where it is based. Left empty, the wizard
    # falls back to the module's default operation type, which is how the
    # single-branch pilot runs.
    x_picking_type_id = fields.Many2one(
        'stock.picking.type', string='Delivery Note Type',
        domain="[('x_is_water_delivery', '=', True)]",
        help='Operation type (and therefore branch and note numbering) used '
             'for delivery notes issued from this tanker.',
    )

    # A "trip" is not a fixed volume: a trailer carries 32 m³ and an Isuzu far
    # less, which is why this is a property of the truck rather than a unit of
    # measure. A UoM factor is a global constant; this one varies per vehicle,
    # so the conversion happens when the note is issued.
    x_capacity_m3 = fields.Float(
        string='Trip Volume (m³)', digits='Product Unit',
        help='How many cubic metres one full trip of this tanker delivers. '
             'Used when the driver enters a quantity in trips instead of m³.',
    )

