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
        Picking = self.env['stock.picking']
        for wizard in self:
            normal, rated = Picking._water_client_scope(
                wizard.driver_id, wizard._picking_type())
            partners = rated if wizard.show_all_clients else normal
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
        Picking = self.env['stock.picking']
        for wizard in self:
            partner = wizard.partner_id._origin
            if not partner:
                wizard.off_route = False
                continue
            normal, _rated = Picking._water_client_scope(
                wizard.driver_id, wizard._picking_type())
            wizard.off_route = partner not in normal

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
    # All three delegate to `stock.picking`, which is where the rules live now
    # that the offline queue issues notes through the same path. The wizard
    # keeps thin wrappers because its computes call them on an unsaved record.
    def _picking_type(self, strict=False):
        self.ensure_one()
        return self.env['stock.picking']._water_picking_type_for(
            self.vehicle_id, strict=strict)

    def _quantity_in_product_uom(self):
        self.ensure_one()
        return self.env['stock.picking']._water_quantity_in_product_uom(
            self.vehicle_id, self.entered_qty, self.entered_uom)

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

        # `location_mode='raise'`: the driver is standing in front of the
        # client, so a failed location check is something he can act on. The
        # offline queue calls the same method with 'hold' for the opposite
        # reason -- see `stock.picking._issue_water_note`.
        picking = self.env['stock.picking']._issue_water_note({
            'partner_id': self.partner_id.id,
            'product_id': self.product_id.id,
            'entered_qty': self.entered_qty,
            'entered_uom': self.entered_uom,
            'driver_id': self.driver_id.id,
            'vehicle_id': self.vehicle_id.id,
            'signature': self.signature,
            'signed_by': self.signed_by,
            'signature_origin': self.signature_origin,
            'gps_latitude': self.gps_latitude,
            'gps_longitude': self.gps_longitude,
            'gps_accuracy': self.gps_accuracy,
            'note': self.note,
            'off_route': self.off_route,
        }, location_mode='raise')

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
