from math import asin, cos, radians, sin, sqrt

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

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

    # ------------------------------------------------------------------
    # Issued from the phone's offline queue rather than against a live
    # connection. A lot of the sites have no signal at all, so the note is
    # captured at the client and sent when the truck comes back into
    # coverage -- which moves two things the server used to observe for
    # itself under the driver's control: the clock, and the delay. So all
    # three timestamps are kept apart: when the phone says it was captured,
    # when the server received it, and (core's own) when it was created.
    x_issued_offline = fields.Boolean(
        string='Issued Offline', index=True, copy=False,
        help='Captured at the client with no connection and sent later.',
    )
    x_captured_at = fields.Datetime(
        string='Captured At', copy=False,
        help="When the driver's phone recorded the delivery. This is the "
             "phone's own clock, so it is kept separate from when the server "
             "received it.",
    )
    x_capture_id = fields.Many2one(
        'ksw.water.capture', string='Offline Capture', copy=False, index='btree_not_null',
        ondelete='set null',
    )

    # The location rule is not dropped for an offline note -- it is applied to
    # the position the phone recorded at the client, which is the same question
    # asked of the same data, just later. What changes is what a failure MEANS:
    # by the time it is checked the water is in the tank and the truck is back,
    # so refusing would recreate the undocumented delivery this whole module
    # exists to eliminate. The note is issued and held unvalidated instead, and
    # a dispatcher decides -- the recorded-acceptance pattern this module
    # already uses for `x_off_route`.
    x_location_exception = fields.Boolean(
        string='Location Not Confirmed', index=True, copy=False,
        help='The note was issued outside the allowed distance from the '
             'client, or with no position at all, and is waiting for a '
             'dispatcher to accept it.',
    )
    x_location_exception_reason = fields.Char(
        string='Why It Is Held', copy=False,
    )

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

    # ------------------------------------------------------------------
    # One issuance path, called from every route in
    # ------------------------------------------------------------------
    # There are two ways a note gets created now -- the driver's wizard against
    # a live connection, and the phone's offline queue when the truck comes
    # back into coverage. They must not be two implementations of the same
    # rules: the whole module is built on "one predicate, called from every
    # route" (KSW gotcha #37/#48), and pricing and validation are exactly where
    # a second copy would quietly drift.
    @api.model
    def _water_picking_type_for(self, vehicle, strict=False):
        """Branch, numbering and the branch rules follow the tanker; the pilot
        branch is the fallback for a tanker not yet assigned one."""
        picking_type = vehicle.sudo().x_picking_type_id if vehicle else False
        if not picking_type:
            picking_type = self.env.ref(
                'KSW_water_delivery.picking_type_water_out', raise_if_not_found=False,
            )
        if not picking_type:
            if not strict:
                return self.env['stock.picking.type']
            raise UserError(_(
                'No water delivery operation type is configured. Set one on the '
                'tanker, or restore the default one.'
            ))
        return picking_type.sudo()

    @api.model
    def _water_quantity_in_product_uom(self, vehicle, entered_qty, entered_uom):
        """A trip is not a unit of measure: a trailer carries 32 m³ and an Isuzu
        far less, so the factor belongs to the truck and the conversion happens
        here rather than in `uom.uom`."""
        if entered_uom == 'trip':
            return entered_qty * ((vehicle.sudo().x_capacity_m3 if vehicle else 0.0) or 0.0)
        return entered_qty

    @api.model
    def _water_location_check(self, picking_type, partner, latitude, longitude):
        """How far the note was issued from the client, and whether that is a
        problem. Returns (distance_or_None, problem_or_None).

        The same question is asked of an offline note as of a live one -- the
        position the phone recorded at the client is the position that gets
        checked. Only WHEN it is asked changes, and therefore what a failure
        can reasonably do about it.
        """
        partner = partner.sudo()
        distance = None
        if latitude and longitude and partner.partner_latitude and partner.partner_longitude:
            distance = haversine_m(latitude, longitude,
                                   partner.partner_latitude, partner.partner_longitude)
        if picking_type.x_location_rule != 'enforce':
            return distance, None
        if not (latitude and longitude):
            return distance, _(
                'No position was recorded for this delivery, and this branch '
                'issues delivery notes at the client\'s site.'
            )
        radius = picking_type.x_location_radius_m or 300
        # A client nobody has located yet stays reachable -- otherwise
        # enforcement would lock out every site until somebody surveys it, and
        # the surveying is done BY issuing notes there.
        if distance is not None and distance > radius:
            return distance, _(
                'Issued %(distance)d m from %(client)s; this branch allows '
                '%(radius)d m.',
                distance=int(distance), client=partner.display_name, radius=radius,
            )
        return distance, None

    @api.model
    def _issue_water_note(self, payload, location_mode='raise'):
        """Create, price and validate one water delivery note.

        `location_mode` is the only thing the two callers differ on:

        * ``'raise'`` -- the live wizard. The driver is standing there, so a
          failed location check is something he can act on: tell him, issue
          nothing.
        * ``'hold'`` -- the offline queue. By the time this runs the water is
          in the customer's tank and the truck is back at the depot, so
          refusing would produce a delivery with no document against it, which
          is the exact failure this module was built to remove. The note is
          created and left UNVALIDATED instead, flagged, and a dispatcher
          decides. Unvalidated is what gives that teeth without inventing a
          reversal: `invoice_policy='delivery'` bills `qty_delivered`, and
          nothing is delivered until somebody validates it.

        Returns the picking. Raises `UserError` for everything that is a
        genuine blocker in both modes -- no rate, no quantity, no operation
        type -- because none of those can be resolved by a dispatcher looking
        at the note either.
        """
        partner = self.env['res.partner'].browse(payload['partner_id'])
        product = self.env['product.product'].browse(payload['product_id'])
        vehicle = self.env['ksw.fleet.vehicle'].browse(payload.get('vehicle_id') or [])
        picking_type = self._water_picking_type_for(vehicle, strict=True)

        # Every rule is read off the operation type, never off what the caller
        # sent: a view-level `invisible=` is cosmetic against a direct RPC, and
        # so is anything a phone puts in a JSON body.
        if picking_type.x_signature_required:
            if not payload.get('signature'):
                raise UserError(_('The customer has to sign before the note can be issued.'))
            if not payload.get('signed_by'):
                raise UserError(_('Record the name of the person who signed.'))

        latitude = payload.get('gps_latitude') or 0.0
        longitude = payload.get('gps_longitude') or 0.0
        distance, location_problem = self._water_location_check(
            picking_type, partner, latitude, longitude)

        if location_problem and location_mode == 'raise':
            if not (latitude and longitude):
                raise UserError(_(
                    'This branch issues delivery notes at the client\'s site, so '
                    'your location is needed. Allow location access in the '
                    'browser and try again.'
                ))
            raise UserError(_(
                'You appear to be %(distance)d m from %(client)s, and notes '
                'for this branch may only be issued within %(radius)d m of '
                'the client. Check you have picked the right client.',
                distance=int(distance or 0), client=partner.display_name,
                radius=picking_type.x_location_radius_m or 300,
            ))
        held = bool(location_problem)

        rate = self.env['ksw.water.rate'].sudo()._rate_for(partner, product)
        if not rate:
            raise UserError(_(
                'There is no agreed rate for %(product)s with %(client)s, so the '
                'note cannot be priced. Ask accounting to add one.',
                product=product.display_name, client=partner.display_name,
            ))

        quantity = self._water_quantity_in_product_uom(
            vehicle, payload.get('entered_qty') or 0.0,
            payload.get('entered_uom') or 'product')
        if quantity <= 0:
            raise UserError(_(
                'The quantity must be greater than zero. If you entered trips, '
                'check that the tanker has a trip volume set.'
            ))

        # The full native chain, on purpose: one trip = one sale order, so that
        # month end is `qty_delivered` on real order lines and the consolidated
        # invoice needs no code of ours at all. The price is the agreed rate --
        # and it is resolved HERE, now, never taken from what the phone sent.
        # A phone carrying a three-day-old snapshot must not be able to invoice
        # a stale rate.
        order = self.env['sale.order'].sudo().create({
            'partner_id': partner.id,
            'company_id': picking_type.company_id.id or self.env.company.id,
            'warehouse_id': picking_type.warehouse_id.id,
            'order_line': [(0, 0, {
                'product_id': product.id,
                'product_uom_qty': quantity,
                'price_unit': rate.price,
                'name': payload.get('note')
                or product.get_product_multiline_description_sale(),
            })],
        })
        order.action_confirm()

        picking = order.picking_ids
        if len(picking) != 1:
            raise UserError(_(
                'Expected exactly one delivery for this order, got %s. Ask a '
                'dispatcher to check the warehouse configuration.', len(picking),
            ))

        # Re-home the delivery onto the branch's own operation type. Core's
        # write() re-draws the note number from that type's sequence and moves
        # the locations across with it (stock_picking.py: write).
        picking.write({'picking_type_id': picking_type.id})
        picking.write({
            'x_driver_id': payload.get('driver_id') or False,
            'x_vehicle_id': vehicle.id or False,
            'x_entered_qty': payload.get('entered_qty') or 0.0,
            'x_entered_uom': payload.get('entered_uom') or 'product',
            'x_gps_latitude': latitude,
            'x_gps_longitude': longitude,
            'x_gps_accuracy': payload.get('gps_accuracy') or 0.0,
            'x_gps_distance_m': distance or 0.0,
            'x_off_route': bool(payload.get('off_route')),
            'x_issued_offline': bool(payload.get('issued_offline')),
            'x_captured_at': payload.get('captured_at') or False,
            'x_location_exception': held,
            'x_location_exception_reason': location_problem or False,
        })

        if held:
            # Deliberately not validated and deliberately not signed. The
            # signature is written when a dispatcher accepts it, so the PDF
            # core attaches still carries validated quantities -- the same
            # ordering rule as the live path, not an exception to it.
            # Markup % dict, never a formatted string: the reason carries the
            # client's display name, which is user data (KSW gotcha #13).
            picking.message_post(body=Markup(
                '<strong>Held for review.</strong><br/>%(reason)s'
            ) % {'reason': location_problem})
            return picking

        picking.move_ids.write({'quantity': quantity, 'picked': True})
        picking.with_context(skip_backorder=True).button_validate()
        self._water_apply_signature(picking, payload)
        return picking

    @api.model
    def _water_apply_signature(self, picking, payload):
        """Signature last, so the PDF that core attaches carries the validated
        quantities. Writing it triggers `stock.picking._attach_sign()`, which
        renders the delivery slip, attaches it and posts it to the chatter --
        that attachment is the signed document, and we add nothing to it.

        During the proof of concept there is no signature, so none of this runs
        and no PDF is attached. The note still exists, still carries its
        figures, and can be signed later through the standard Sign button --
        the signing half is off, not absent.
        """
        if payload.get('signature'):
            picking.write({
                'signature': payload['signature'],
                'x_signed_by': payload.get('signed_by'),
                'x_signed_on': payload.get('captured_at') or fields.Datetime.now(),
                'x_signature_origin': payload.get('signature_origin') or 'on_site',
            })
        elif payload.get('signed_by'):
            picking.write({'x_signed_by': payload['signed_by']})

    @api.model
    def _water_client_scope(self, driver, picking_type):
        """The driver's normal client list, and the full priced list behind it.

        Narrowed three times over, each for a different reason. Widest is
        "clients we can price" -- anything else cannot produce a note. Then the
        branch: a driver working out of 172 has no business seeing branch 120's
        97 clients, and that alone takes the list from 312 to somewhere between
        9 and 97. Then the driver's own list, if somebody has filled one in.

        Precedence is narrowest-wins, but a MISSING list never narrows: a branch
        nobody has listed yet must not lock its drivers out.

        Returns (normal, rated). Both are needed by both callers -- the wizard
        shows `rated` behind the "client not listed" tick, and the phone has to
        cache both because offline is exactly when it cannot go and ask.
        """
        Rate = self.env['ksw.water.rate'].sudo()
        Coverage = self.env['ksw.water.client.branch'].sudo()
        rated = self.env['res.partner'].browse(Rate._rated_partner_ids())
        normal = rated
        driver_clients = driver.sudo().x_water_client_ids if driver else False
        if driver_clients:
            normal = rated & driver_clients
        elif picking_type:
            branch_ids = Coverage._partner_ids_for_branch(picking_type.x_branch_code)
            if branch_ids is not None:
                normal = rated & self.env['res.partner'].browse(branch_ids)
        return normal, rated
