from math import asin, cos, radians, sin, sqrt

from odoo import api, fields, models

_EARTH_RADIUS_M = 6371000.0


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres. Good to well under a metre at the
    distances involved here, and needs no library."""
    p1, p2 = radians(lat1), radians(lat2)
    dp, dl = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * asin(sqrt(a))


class StockPickingType(models.Model):
    _inherit = 'stock.picking.type'

    x_is_water_delivery = fields.Boolean(
        string='Water Delivery Note',
        help='Operation types flagged here produce a priced, signable water '
             'delivery note instead of an ordinary delivery slip.',
    )
    # The branch dimension in BAS is CODE2 on the document, not an attribute of
    # the customer -- so it belongs on the operation type (one per branch),
    # which is also what gives each branch its own note numbering.
    x_branch_code = fields.Char(string='Branch (CODE2)')
    # Off during the proof of concept: drivers issue the note at the customer to
    # prove they can, and the paper note still collects the wet signature. The
    # signing half of the module is built and tested -- ticking this box turns it
    # on for this branch, and nothing else has to change.
    x_signature_required = fields.Boolean(
        string='Require a Signature', default=True,
        help='When off, a driver can issue the delivery note without capturing '
             'a signature. Notes issued while this is off are not counted as '
             'missing a signature.',
    )
    # Staged on purpose. No client in the database has coordinates yet, so
    # "enforce" on day one would stop every delivery. "Capture" records where
    # the driver was, which is both useful on its own and the only way the
    # client locations ever get known -- then the branch switches to "enforce".
    x_location_rule = fields.Selection(
        [('off', 'Do not record'),
         ('capture', 'Record where the driver was'),
         ('enforce', 'Require the driver to be at the client')],
        string='Driver Location', default='capture', required=True,
    )
    x_location_radius_m = fields.Integer(
        string='Allowed Distance (m)', default=300,
        help='How far from the client\'s recorded location a delivery note may '
             'be issued. Only applied to clients whose location is known.',
    )


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    # Stamped from the operation type at creation, then left alone. Deliberately
    # NOT `related=`: a signed note is a document, and a document records what
    # was true when it was issued. Re-pointing an operation type at another
    # branch later must not silently re-file every note already signed under the
    # old one -- and `x_is_water_delivery` carries the record rules, so a related
    # field would retroactively move notes in and out of people's scope.
    x_is_water_delivery = fields.Boolean(string='Water Delivery Note', index=True, copy=False)
    x_branch_code = fields.Char(string='Branch (CODE2)', index=True, copy=False)
    # Stamped for the same reason: whether a signature was expected is a fact
    # about the day the note was issued. Without it, turning signing on at the
    # end of the proof of concept would retrospectively mark every POC note as
    # missing a signature.
    x_signature_required = fields.Boolean(
        string='Signature Required', index=True, copy=False,
    )

    x_driver_id = fields.Many2one(
        'hr.employee', string='Driver', index=True, copy=False,
        help='The driver who delivered the load and collected the signature.',
    )
    x_vehicle_id = fields.Many2one(
        'ksw.fleet.vehicle', string='Tanker', copy=False,
    )
    # `stock.picking` carries a `signature` image but -- unlike `sale.order` --
    # no record of who signed it or when. The paper note has both, so we keep
    # both.
    x_signed_by = fields.Char(string='Signed By', copy=False)
    x_signed_on = fields.Datetime(string='Signed On', copy=False)

    # Signed at the customer, or captured later at the depot. The paper process
    # cannot tell these apart; this one can, and month-end should see it.
    x_signature_origin = fields.Selection(
        [('on_site', 'Signed at the customer'),
         ('depot', 'Signed at the depot')],
        string='Signature Captured', copy=False,
    )

    # Where the driver was when he issued it. Captured from the phone's own
    # geolocation, so it says the delivery happened at the site rather than at
    # the depot afterwards -- the thing the paper note can never show.
    x_gps_latitude = fields.Float(string='Latitude', digits=(10, 7), copy=False)
    x_gps_longitude = fields.Float(string='Longitude', digits=(10, 7), copy=False)
    x_gps_accuracy = fields.Float(string='GPS Accuracy (m)', copy=False)
    x_gps_distance_m = fields.Float(
        string='Distance from Client (m)', copy=False,
        help='Distance between where the note was issued and the client\'s '
             'recorded location. Empty when the client has no location yet.',
    )

    # What the driver typed, kept next to what it converted to. The sale order
    # line is always in the product's own unit; this records that he said
    # "2 trips" rather than "64 m³", which is what he will recognise if the
    # note is ever queried.
    x_entered_qty = fields.Float(string='Entered Quantity', digits='Product Unit', copy=False)
    x_entered_uom = fields.Selection(
        [('product', 'Product unit'), ('trip', 'Trips')],
        string='Entered In', copy=False,
    )

    # Issued to a client outside the list this driver's branch normally serves.
    # Allowed, and recorded: a narrowed picker that cannot be escaped becomes a
    # driver stuck at a gate, and an exception nobody can see is worse than no
    # rule at all.
    x_off_route = fields.Boolean(string='Off the Usual Route', index=True, copy=False)

    # The note's own figures, mirrored off the sale order under sudo so that a
    # driver can read his own note and a billing clerk can total a month
    # without either of them needing access to `sale.order` (KSW pattern: a
    # display field that spares a role another model's access must not then
    # reach into it as that role).
    x_currency_id = fields.Many2one(related='company_id.currency_id', store=True)
    x_load_qty = fields.Float(
        string='Loads', compute='_compute_water_amounts',
        store=True, compute_sudo=True, digits='Product Unit',
    )
    x_amount_untaxed = fields.Monetary(
        string='Amount Before Tax', currency_field='x_currency_id',
        compute='_compute_water_amounts', store=True, compute_sudo=True,
    )
    x_amount_tax = fields.Monetary(
        string='VAT', currency_field='x_currency_id',
        compute='_compute_water_amounts', store=True, compute_sudo=True,
    )
    x_amount_total = fields.Monetary(
        string='Amount After Tax', currency_field='x_currency_id',
        compute='_compute_water_amounts', store=True, compute_sudo=True,
    )

    @api.model_create_multi
    def create(self, vals_list):
        """Stamp the classification on every route in, not just the wizard: a
        dispatcher creating a note by hand in Inventory must land in the same
        scope, since the record rules read `x_is_water_delivery`."""
        # The operation type can arrive from the context default rather than
        # from vals, exactly as core's own create() has to allow for.
        default_type_id = self.default_get(['picking_type_id']).get('picking_type_id')
        type_ids = {
            vals.get('picking_type_id', default_type_id) for vals in vals_list
        } - {False, None}
        by_id = {
            ptype.id: ptype
            for ptype in self.env['stock.picking.type'].browse(type_ids).sudo()
        }
        for vals in vals_list:
            ptype = by_id.get(vals.get('picking_type_id', default_type_id))
            if ptype and ptype.x_is_water_delivery:
                vals.setdefault('x_is_water_delivery', True)
                vals.setdefault('x_branch_code', ptype.x_branch_code)
                vals.setdefault('x_signature_required', ptype.x_signature_required)
        return super().create(vals_list)

    def write(self, vals):
        """`is_signed` is a non-stored compute, so nothing can search for the
        gap this whole module exists to close: a delivery that was issued and
        never signed. `x_signed_on` is that marker, and it has to be stamped on
        every route into a signature -- the wizard, and the native Sign button
        a dispatcher uses on a note that came back unsigned."""
        if vals.get('picking_type_id'):
            # The wizard re-homes the delivery onto the branch's operation type
            # after the sale order created it under the warehouse's default, so
            # the stamp has to happen here as well as in create().
            ptype = self.env['stock.picking.type'].browse(vals['picking_type_id']).sudo()
            if ptype.x_is_water_delivery:
                vals.setdefault('x_is_water_delivery', True)
                vals.setdefault('x_branch_code', ptype.x_branch_code)
                vals.setdefault('x_signature_required', ptype.x_signature_required)
        if 'signature' in vals:
            if vals['signature']:
                vals.setdefault('x_signed_on', fields.Datetime.now())
            else:
                vals.setdefault('x_signed_on', False)
                vals.setdefault('x_signature_origin', False)
        res = super().write(vals)
        if vals.get('signature'):
            # The native Sign widget writes the signature alone; the name it
            # collected is the partner's, which is core's own default.
            for picking in self:
                if not picking.x_signed_by and picking.partner_id:
                    picking.x_signed_by = picking.partner_id.name
        return res

    @api.depends('x_is_water_delivery', 'state', 'move_ids.quantity',
                 'move_ids.product_uom_qty', 'sale_id.amount_untaxed',
                 'sale_id.amount_tax', 'sale_id.amount_total')
    def _compute_water_amounts(self):
        for picking in self:
            if not picking.x_is_water_delivery:
                picking.x_load_qty = 0.0
                picking.x_amount_untaxed = 0.0
                picking.x_amount_tax = 0.0
                picking.x_amount_total = 0.0
                continue
            moves = picking.move_ids
            if picking.state == 'done':
                picking.x_load_qty = sum(moves.mapped('quantity'))
            else:
                picking.x_load_qty = sum(moves.mapped('product_uom_qty'))
            order = picking.sale_id
            picking.x_amount_untaxed = order.amount_untaxed
            picking.x_amount_tax = order.amount_tax
            picking.x_amount_total = order.amount_total
