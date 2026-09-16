from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.stock_picking import haversine_m

DRIVER_GROUP = 'KSW_water_delivery.group_water_driver'
DISPATCHER_GROUP = 'KSW_water_delivery.group_water_dispatcher'


class KswWaterDeliveryWizard(models.TransientModel):
    _name = 'ksw.water.delivery.wizard'
    _description = 'Water Delivery Note'

    # --- what the driver actually fills in -------------------------------
    partner_id = fields.Many2one(
        'res.partner', string='Client', required=True,
        domain="[('id', 'in', allowed_partner_ids)]",
    )
    # A plain field, NOT a stored compute. `required=True` on a stored computed
    # field makes create() impossible -- the INSERT goes in before the compute
    # runs (KSW gotcha #145), and `precompute=True` does not save it here
    # because the compute depends on a non-stored computed field. It fails
    # exactly where it hurts: the driver taps Issue and gets a raw
    # "mandatory field is not set" from Postgres.
    #
    # Preselection now happens in the onchange (for the screen) and in create()
    # (for everything else, including the client omitting a readonly field from
    # its payload -- which is what actually broke it).
    product_id = fields.Many2one(
        'product.product', string='Product', required=True,
        domain="[('id', 'in', allowed_product_ids)]",
    )
    entered_qty = fields.Float(string='Quantity', default=1.0, required=True,
                               digits='Product Unit')
    entered_uom = fields.Selection(
        [('product', 'Product unit'), ('trip', 'Trips')],
        string='Entered In', default='product', required=True,
    )
    signature = fields.Binary(string='Customer Signature', copy=False)
    signed_by = fields.Char(string='Signed By')
    signature_origin = fields.Selection(
        [('on_site', 'Signed at the customer'),
         ('depot', 'Signed at the depot')],
        string='Signature Captured', default='on_site', required=True,
    )
    note = fields.Char(string='Remark')

    # --- filled in for him, but visible so he can correct a swapped truck --
    driver_id = fields.Many2one(
        'hr.employee', string='Driver', required=True,
        default=lambda self: self._default_driver_id(),
    )
    vehicle_id = fields.Many2one(
        'ksw.fleet.vehicle', string='Tanker',
        default=lambda self: self._default_vehicle_id(),
    )

    # --- what the phone reports -------------------------------------------
    gps_latitude = fields.Float(digits=(10, 7))
    gps_longitude = fields.Float(digits=(10, 7))
    gps_accuracy = fields.Float()

    # --- everything below is worked out for him ---------------------------
    # Relational + domain rather than a dynamic `selection=`: the options vary
    # per record (per client), and the web client strips the context out of
    # get_views and caches the result (KSW gotcha #39).
    allowed_partner_ids = fields.Many2many(
        'res.partner', 'ksw_water_wizard_partner_rel',
        compute='_compute_allowed_partner_ids',
    )
    # A narrowed list must not become a dead end: a driver really is sent to a
    # client outside his branch's list sometimes, and being stuck at the gate
    # is worse than the wide list was. So the way out is one tick -- and it
    # stamps the note (`x_off_route`), because the useful thing about an
    # exception is being able to see it afterwards.
    show_all_clients = fields.Boolean(
        string='Client not listed',
        help='Show every client that has an agreed rate, not just the ones '
             'this branch normally serves. The note records that it was issued '
             'off the usual route.',
    )
    off_route = fields.Boolean(compute='_compute_off_route')
    allowed_product_ids = fields.Many2many(
        'product.product', 'ksw_water_wizard_product_rel',
        compute='_compute_allowed_product_ids',
    )
    product_locked = fields.Boolean(compute='_compute_allowed_product_ids')
    signature_required = fields.Boolean(compute='_compute_branch_rules')
    location_rule = fields.Selection(
        [('off', 'Do not record'), ('capture', 'Record'), ('enforce', 'Enforce')],
        compute='_compute_branch_rules',
    )
    trip_volume_m3 = fields.Float(related='vehicle_id.x_capacity_m3', string='Trip Volume (m³)')
    can_enter_trips = fields.Boolean(compute='_compute_quantity_preview')
    quantity_preview = fields.Char(compute='_compute_quantity_preview')
    rate_preview = fields.Char(compute='_compute_quantity_preview')

    picking_id = fields.Many2one('stock.picking', string='Delivery Note', readonly=True)

    # --- defaults ---------------------------------------------------------
    @api.model
    def _default_driver_id(self):
        # sudo(): a driver has no read access to hr.employee as a model, but
        # his own identity is not privileged information.
        return self.env.user.sudo().employee_id

    @api.model
    def _default_vehicle_id(self):
        employee = self.env.user.sudo().employee_id
        if not employee:
            return False
        return self.env['ksw.fleet.vehicle'].sudo().search(
            [('driver_id', '=', employee.id), ('state', '=', 'confirmed')], limit=1,
        )

    # --- what he may pick -------------------------------------------------
    @api.depends('gps_latitude', 'gps_longitude', 'show_all_clients',
                 'driver_id', 'vehicle_id')
    def _compute_allowed_partner_ids(self):
        """Narrowed three times over, each for a different reason.

        Widest is "clients we can price" -- anything else cannot produce a note.
        Then the branch: a driver working out of 172 has no business seeing
        branch 120's 97 clients, and that alone takes the list from 312 to
        somewhere between 9 and 97. Then the driver's own list, if somebody has
        filled one in.

        Precedence is narrowest-wins, but a MISSING list never narrows: a branch
        nobody has listed yet must not lock its drivers out.
        """
        Rate = self.env['ksw.water.rate'].sudo()
        Coverage = self.env['ksw.water.client.branch'].sudo()
        rated = self.env['res.partner'].browse(Rate._rated_partner_ids())
        for wizard in self:
            partners = rated
            if not wizard.show_all_clients:
                driver_clients = wizard.driver_id.sudo().x_water_client_ids
                if driver_clients:
                    partners &= driver_clients
                else:
                    branch_ids = Coverage._partner_ids_for_branch(
                        wizard._picking_type().x_branch_code)
                    if branch_ids is not None:
                        partners &= self.env['res.partner'].browse(branch_ids)
            if wizard.location_rule == 'enforce' and wizard.gps_latitude:
                radius = wizard._picking_type().x_location_radius_m or 300
                near = partners.filtered(lambda p: (
                    p.partner_latitude and p.partner_longitude
                    and haversine_m(wizard.gps_latitude, wizard.gps_longitude,
                                    p.partner_latitude, p.partner_longitude) <= radius
                ))
                # A client whose location nobody has recorded yet must stay
                # reachable, or enforcement would lock out every site until
                # somebody surveys it -- and the surveying is done BY issuing
                # notes there.
                unlocated = partners.filtered(
                    lambda p: not (p.partner_latitude and p.partner_longitude))
                partners = near | unlocated
            wizard.allowed_partner_ids = partners

    @api.depends('partner_id', 'driver_id', 'vehicle_id')
    def _compute_off_route(self):
        """Is the chosen client outside the list he would normally see?
        Worked out from the lists themselves rather than from the tick box, so
        ticking it and then picking a normal client is not an exception."""
        Coverage = self.env['ksw.water.client.branch'].sudo()
        for wizard in self:
            partner = wizard.partner_id._origin
            if not partner:
                wizard.off_route = False
                continue
            driver_clients = wizard.driver_id.sudo().x_water_client_ids
            if driver_clients:
                wizard.off_route = partner not in driver_clients
                continue
            branch_ids = Coverage._partner_ids_for_branch(
                wizard._picking_type().x_branch_code)
            wizard.off_route = branch_ids is not None and partner.id not in branch_ids

    @api.depends('partner_id')
    def _compute_allowed_product_ids(self):
        Rate = self.env['ksw.water.rate'].sudo()
        for wizard in self:
            products = Rate._products_for_partner(wizard.partner_id)
            wizard.allowed_product_ids = products
            wizard.product_locked = len(products) == 1

    @api.depends('vehicle_id')
    def _compute_branch_rules(self):
        # Runs during onchange, so it must never raise -- an unconfigured
        # tanker is action_confirm's problem to report, not something for the
        # form to blow up on while the driver is still filling it in.
        for wizard in self:
            picking_type = wizard._picking_type()
            wizard.signature_required = (
                picking_type.x_signature_required if picking_type else True
            )
            wizard.location_rule = (
                picking_type.x_location_rule if picking_type else 'capture'
            )

    @api.depends('product_id', 'entered_qty', 'entered_uom', 'vehicle_id', 'partner_id')
    def _compute_quantity_preview(self):
        Rate = self.env['ksw.water.rate'].sudo()
        for wizard in self:
            uom = wizard.product_id.uom_id
            # Trips only make sense for a product measured by volume. A product
            # BAS already sells by the trip is entered in its own unit.
            wizard.can_enter_trips = bool(
                wizard.vehicle_id.x_capacity_m3 and uom and uom.name == 'm³'
            )
            qty = wizard._quantity_in_product_uom()
            wizard.quantity_preview = (
                '%(qty)s %(uom)s' % {'qty': f'{qty:g}', 'uom': uom.name or ''}
                if qty and uom else ''
            )
            rate = Rate._rate_for(wizard.partner_id, wizard.product_id)
            if rate:
                wizard.rate_preview = '%(price)s / %(uom)s' % {
                    'price': f'{rate.price:g}', 'uom': uom.name or '',
                }
            else:
                wizard.rate_preview = ''

    @api.onchange('partner_id')
    def _onchange_partner_id(self):
        # The customer's own name is the usual signatory; he overtypes it when
        # a site foreman signs instead.
        if self.partner_id and not self.signed_by:
            self.signed_by = self.partner_id.name
        # 257 of 312 clients buy exactly one product. Offering a picker with one
        # option in it is a question with one answer -- preselect it (KSW UX
        # rule). create() does the same server-side, because a readonly field
        # never reaches the server from the web client.
        products = self.env['ksw.water.rate']._products_for_partner(self.partner_id)
        if self.product_id._origin not in products:
            self.product_id = products[:1]

    @api.model_create_multi
    def create(self, vals_list):
        """Fill in the product the client could not send.

        When there is one product the form shows it read-only, and the web
        client omits read-only fields from its payload -- so `product_id`
        arrives empty and `required=True` fails at the database, with a message
        no driver can act on. Only auto-filled when the client buys exactly one
        product: with a real choice to make, an empty value is a genuine
        omission and the form should say so.
        """
        Rate = self.env['ksw.water.rate']
        Partner = self.env['res.partner']
        for vals in vals_list:
            if vals.get('product_id') or not vals.get('partner_id'):
                continue
            products = Rate._products_for_partner(Partner.browse(vals['partner_id']))
            if len(products) == 1:
                vals['product_id'] = products.id
        return super().create(vals_list)

    # --- helpers ----------------------------------------------------------
    def _picking_type(self, strict=False):
        """Branch, numbering and the two branch rules follow the tanker; the
        pilot branch is the fallback for a tanker not yet assigned one."""
        self.ensure_one()
        picking_type = self.vehicle_id.sudo().x_picking_type_id
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

    def _quantity_in_product_uom(self):
        """A trip is not a unit of measure: a trailer carries 32 m³ and an Isuzu
        far less, so the factor belongs to the truck and the conversion happens
        here rather than in `uom.uom`."""
        self.ensure_one()
        if self.entered_uom == 'trip':
            return self.entered_qty * (self.vehicle_id.x_capacity_m3 or 0.0)
        return self.entered_qty

    def _distance_to_client_m(self):
        self.ensure_one()
        partner = self.partner_id
        if not (self.gps_latitude and self.gps_longitude
                and partner.partner_latitude and partner.partner_longitude):
            return None
        return haversine_m(self.gps_latitude, self.gps_longitude,
                           partner.partner_latitude, partner.partner_longitude)

    # --- authority --------------------------------------------------------
    def _check_authority(self):
        """Every write below runs under sudo(), so authority is checked here,
        once, before any of it happens (see KSW gotchas #15 / #26)."""
        self.ensure_one()
        if self.env.su:
            return
        user = self.env.user
        is_dispatcher = user.has_group(DISPATCHER_GROUP)
        if not (is_dispatcher or user.has_group(DRIVER_GROUP)):
            raise UserError(_('You are not allowed to issue water delivery notes.'))
        if not is_dispatcher:
            employee = user.sudo().employee_id
            if not employee:
                raise UserError(_(
                    'Your user account is not linked to an employee record, so '
                    'the delivery note cannot record who delivered it. Ask HR '
                    'to link them.'
                ))
            if self.driver_id != employee:
                raise UserError(_('A driver can only record his own deliveries.'))

    # --- the one button ---------------------------------------------------
    def action_confirm(self):
        self.ensure_one()
        self._check_authority()

        if self.picking_id:
            raise UserError(_(
                'This delivery note has already been issued as %s.',
                self.picking_id.name,
            ))

        picking_type = self._picking_type(strict=True)

        # Read every rule off the operation type, not off the form: the form's
        # computed flags decide what the driver SEES, and a view-level
        # `invisible=` is cosmetic against a direct RPC call.
        if picking_type.x_signature_required:
            if not self.signature:
                raise UserError(_('The customer has to sign before the note can be issued.'))
            if not self.signed_by:
                raise UserError(_('Record the name of the person who signed.'))

        distance = self._distance_to_client_m()
        if picking_type.x_location_rule == 'enforce':
            if not (self.gps_latitude and self.gps_longitude):
                raise UserError(_(
                    'This branch issues delivery notes at the client\'s site, so '
                    'your location is needed. Allow location access in the '
                    'browser and try again.'
                ))
            radius = picking_type.x_location_radius_m or 300
            if distance is not None and distance > radius:
                raise UserError(_(
                    'You appear to be %(distance)d m from %(client)s, and notes '
                    'for this branch may only be issued within %(radius)d m of '
                    'the client. Check you have picked the right client.',
                    distance=int(distance), client=self.partner_id.display_name,
                    radius=radius,
                ))

        rate = self.env['ksw.water.rate'].sudo()._rate_for(self.partner_id, self.product_id)
        if not rate:
            raise UserError(_(
                'There is no agreed rate for %(product)s with %(client)s, so the '
                'note cannot be priced. Ask accounting to add one.',
                product=self.product_id.display_name,
                client=self.partner_id.display_name,
            ))

        quantity = self._quantity_in_product_uom()
        if quantity <= 0:
            raise UserError(_(
                'The quantity must be greater than zero. If you entered trips, '
                'check that the tanker has a trip volume set.'
            ))

        # The full native chain, on purpose: one trip = one sale order, so that
        # month end is `qty_delivered` on real order lines and the consolidated
        # invoice needs no code of ours at all. The price is the agreed rate --
        # the driver never sees or types one.
        order = self.env['sale.order'].sudo().create({
            'partner_id': self.partner_id.id,
            'company_id': picking_type.company_id.id or self.env.company.id,
            'warehouse_id': picking_type.warehouse_id.id,
            'order_line': [(0, 0, {
                'product_id': self.product_id.id,
                'product_uom_qty': quantity,
                'price_unit': rate.price,
                'name': self.note or self.product_id.get_product_multiline_description_sale(),
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
            'x_driver_id': self.driver_id.id,
            'x_vehicle_id': self.vehicle_id.id,
            'x_entered_qty': self.entered_qty,
            'x_entered_uom': self.entered_uom,
            'x_gps_latitude': self.gps_latitude,
            'x_gps_longitude': self.gps_longitude,
            'x_gps_accuracy': self.gps_accuracy,
            'x_gps_distance_m': distance or 0.0,
            'x_off_route': self.off_route,
        })

        picking.move_ids.write({'quantity': quantity, 'picked': True})
        picking.with_context(skip_backorder=True).button_validate()

        # Signature last, so the PDF that core attaches carries the validated
        # quantities. Writing it triggers stock.picking._attach_sign(), which
        # renders the delivery slip, attaches it and posts to the chatter --
        # that attachment is the signed document, and we add nothing to it.
        #
        # During the proof of concept there is no signature, so none of this
        # runs and no PDF is attached. The note still exists, still carries its
        # figures, and can be signed later through the standard Sign button --
        # the signing half is off, not absent.
        if self.signature:
            picking.write({
                'signature': self.signature,
                'x_signed_by': self.signed_by,
                'x_signed_on': fields.Datetime.now(),
                'x_signature_origin': self.signature_origin,
            })
        elif self.signed_by:
            picking.write({'x_signed_by': self.signed_by})

        self.picking_id = picking.id
        return self._action_open_note(picking)

    def _action_open_note(self, picking):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Delivery Note'),
            'res_model': 'stock.picking',
            'res_id': picking.id,
            'view_mode': 'form',
            'views': [(
                self.env.ref('KSW_water_delivery.view_water_delivery_note_form').id,
                'form',
            )],
            'target': 'current',
        }
