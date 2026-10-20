from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

DISPATCH_GROUP = 'KSW_water_delivery.group_water_dispatch'
DRIVER_GROUP = 'KSW_water_delivery.group_water_driver'

# What the dispatcher decides. Frozen once the dispatch leaves draft: the
# driver's phone may already be holding it offline, so changing it under him
# would deliver one thing and record another.
_ORDER_FIELDS = {
    'picking_type_id', 'partner_id', 'product_id', 'entered_qty',
    'entered_uom', 'driver_user_id', 'vehicle_id', 'note',
}


class KswWaterDispatch(models.Model):
    """One delivery ordered by the branch's dispatcher, for one driver.

    Only used on a branch whose operation type is switched to
    ``x_issue_mode = 'dispatch'``. There the dispatcher decides everything
    -- client, product, quantity, driver -- and the driver's app shows him
    the order with one button: he presses it at the client's site and the
    note is issued through the same capture queue as every other note, so
    the location rule, the offline queue and the supervisor's hold all
    apply unchanged. On a branch left on ``'driver'`` this model is unused.
    """
    _name = 'ksw.water.dispatch'
    _description = 'Water Delivery Dispatch'
    _inherit = ['mail.thread']
    _order = 'id desc'

    name = fields.Char(string='Dispatch', required=True, copy=False,
                       readonly=True, default=lambda self: _('New'))
    state = fields.Selection(
        [('draft', 'Draft'),
         ('dispatched', 'Dispatched'),
         ('delivered', 'Delivered'),
         ('cancelled', 'Cancelled')],
        default='draft', required=True, index=True, tracking=True, copy=False,
    )

    picking_type_id = fields.Many2one(
        'stock.picking.type', string='Branch', required=True, index=True,
        default=lambda self: self._default_picking_type_id(),
        domain="[('id', 'in', allowed_picking_type_ids)]", tracking=True,
    )
    partner_id = fields.Many2one(
        'res.partner', string='Client', required=True, index=True,
        domain="[('id', 'in', allowed_partner_ids)]", tracking=True,
    )
    # Plain, not a stored compute with required=True (KSW gotcha #145): the
    # one-product preselect happens in the onchange AND in create().
    product_id = fields.Many2one(
        'product.product', string='Product', required=True,
        domain="[('id', 'in', allowed_product_ids)]", tracking=True,
    )
    entered_qty = fields.Float(string='Quantity', digits='Product Unit',
                               default=1.0, required=True, tracking=True)
    entered_uom = fields.Selection(
        [('product', 'Product unit'), ('trip', 'Trips')],
        string='Entered In', default='product', required=True,
    )
    # The driver is picked as a USER, not an employee: a dispatcher has no HR
    # rights, so an hr.employee picker would be answered by hr.employee.public
    # and a domain over hr.employee ids would raise on read (gotcha #34 and
    # the "picker wider than read scope" lesson). The employee is derived.
    driver_user_id = fields.Many2one(
        'res.users', string='Driver', index=True,
        domain="[('id', 'in', allowed_driver_user_ids)]", tracking=True,
    )
    driver_id = fields.Many2one(
        'hr.employee', string='Driver (Employee)', index=True,
        compute='_compute_driver_id', store=True, compute_sudo=True,
    )
    # Fetched from the fleet assignment, never chosen -- the same rule as the
    # driver's own app. Stamped at dispatch: the trip volume it carries is
    # what "2 trips" meant when the dispatcher wrote it.
    vehicle_id = fields.Many2one('ksw.fleet.vehicle', string='Tanker', readonly=True)
    note = fields.Char(string='Instructions')

    dispatcher_id = fields.Many2one('res.users', string='Dispatched By',
                                    readonly=True, copy=False)
    dispatched_on = fields.Datetime(string='Dispatched On', readonly=True, copy=False)

    # --- the outcome -------------------------------------------------------
    capture_id = fields.Many2one('ksw.water.capture', string='Driver Confirmation',
                                 readonly=True, copy=False, index='btree_not_null')
    picking_id = fields.Many2one('stock.picking', string='Delivery Note',
                                 readonly=True, copy=False)
    delivered_on = fields.Datetime(string='Delivered On', readonly=True, copy=False)
    # Mirrors under sudo: a dispatcher reads the outcome without stock rights.
    note_state = fields.Selection(related='picking_id.state', string='Note Status')
    note_held = fields.Boolean(related='picking_id.x_location_exception',
                               string='Waiting for Supervisor')

    company_id = fields.Many2one('res.company', default=lambda self: self.env.company,
                                 required=True)

    # --- pickers ----------------------------------------------------------
    allowed_picking_type_ids = fields.Many2many(
        'stock.picking.type', compute='_compute_allowed_picking_type_ids')
    allowed_partner_ids = fields.Many2many(
        'res.partner', compute='_compute_allowed_partner_ids')
    allowed_product_ids = fields.Many2many(
        'product.product', compute='_compute_allowed_product_ids')
    allowed_driver_user_ids = fields.Many2many(
        'res.users', compute='_compute_allowed_driver_user_ids')
    product_locked = fields.Boolean(compute='_compute_allowed_product_ids')
    quantity_m3 = fields.Float(string='Delivers (m³)', compute='_compute_quantity_m3',
                               digits='Product Unit')
    client_located = fields.Boolean(compute='_compute_client_located', compute_sudo=True)
    branch_in_dispatch_mode = fields.Boolean(compute='_compute_branch_in_dispatch_mode')

    # ------------------------------------------------------------------
    # Scope
    # ------------------------------------------------------------------
    @api.model
    def _my_branches(self):
        """Water branches this user dispatches for. Administrators all."""
        Type = self.env['stock.picking.type'].sudo()
        domain = [('x_is_water_delivery', '=', True)]
        if not (self.env.su or self.env.user.has_group('base.group_system')):
            domain.append(('x_dispatcher_user_ids', 'in', self.env.uid))
        return Type.search(domain)

    @api.model
    def _default_picking_type_id(self):
        branches = self._my_branches()
        return branches if len(branches) == 1 else False

    def _check_branch_authority(self):
        if self.env.su or self.env.user.has_group('base.group_system'):
            return
        if not self.env.user.has_group(DISPATCH_GROUP):
            raise UserError(_('Only a dispatcher can do this.'))
        for dispatch in self:
            if self.env.user not in dispatch.picking_type_id.sudo().x_dispatcher_user_ids:
                raise UserError(_(
                    'You are not a dispatcher for %s.',
                    dispatch.picking_type_id.sudo().display_name))

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends_context('uid')
    def _compute_allowed_picking_type_ids(self):
        branches = self._my_branches()
        for dispatch in self:
            dispatch.allowed_picking_type_ids = branches

    @api.depends('picking_type_id')
    def _compute_allowed_partner_ids(self):
        """The branch's priced clients. No per-driver narrowing here: the
        driver is chosen afterwards, and the dispatcher's choice of client
        is itself the authority the driver's own list stands in for."""
        Picking = self.env['stock.picking'].sudo()
        for dispatch in self:
            if not dispatch.picking_type_id:
                dispatch.allowed_partner_ids = False
                continue
            normal, _rated = Picking._water_client_scope(
                False, dispatch.picking_type_id._origin.sudo())
            dispatch.allowed_partner_ids = normal

    @api.depends('partner_id')
    def _compute_allowed_product_ids(self):
        Rate = self.env['ksw.water.rate'].sudo()
        for dispatch in self:
            # _origin: on an unsaved form the partner is NewId-wrapped and a
            # search on it matches nothing (KSW gotcha #149).
            partner = dispatch.partner_id._origin
            products = Rate.search([('partner_id', '=', partner.id)]).product_id \
                if partner else self.env['product.product']
            dispatch.allowed_product_ids = products
            dispatch.product_locked = len(products) == 1

    @api.depends('picking_type_id')
    def _compute_allowed_driver_user_ids(self):
        """Drivers whose assigned tanker writes into this branch. The branch
        follows the truck, so a driver on another branch's tanker would issue
        the note into that branch's numbering and rules."""
        Picking = self.env['stock.picking'].sudo()
        vehicles = self.env['ksw.fleet.vehicle'].sudo().search([
            ('state', '=', 'confirmed'), ('driver_id.user_id', '!=', False)])
        for dispatch in self:
            branch = dispatch.picking_type_id._origin
            users = self.env['res.users']
            if branch:
                for vehicle in vehicles:
                    if Picking._water_picking_type_for(vehicle) == branch:
                        users |= vehicle.driver_id.user_id
                users = users.filtered(lambda u: u.has_group(DRIVER_GROUP))
            dispatch.allowed_driver_user_ids = users

    @api.depends('driver_user_id')
    def _compute_driver_id(self):
        for dispatch in self:
            dispatch.driver_id = dispatch.driver_user_id.employee_id

    @api.depends('entered_qty', 'entered_uom', 'vehicle_id')
    def _compute_quantity_m3(self):
        Picking = self.env['stock.picking']
        for dispatch in self:
            dispatch.quantity_m3 = Picking._water_quantity_in_product_uom(
                dispatch.vehicle_id, dispatch.entered_qty, dispatch.entered_uom)

    @api.depends('partner_id')
    def _compute_client_located(self):
        for dispatch in self:
            partner = dispatch.partner_id
            dispatch.client_located = bool(
                partner.partner_latitude or partner.partner_longitude)

    @api.depends('picking_type_id')
    def _compute_branch_in_dispatch_mode(self):
        for dispatch in self:
            dispatch.branch_in_dispatch_mode = \
                dispatch.picking_type_id.sudo().x_issue_mode == 'dispatch'

    # ------------------------------------------------------------------
    # Onchanges and create
    # ------------------------------------------------------------------
    @api.onchange('picking_type_id')
    def _onchange_picking_type_id(self):
        if self.partner_id and self.partner_id._origin not in self.allowed_partner_ids._origin:
            self.partner_id = False
        if self.driver_user_id and self.driver_user_id._origin not in \
                self.allowed_driver_user_ids._origin:
            self.driver_user_id = False

    @api.onchange('partner_id')
    def _onchange_partner_id(self):
        products = self.allowed_product_ids
        if self.product_id._origin not in products._origin:
            self.product_id = products[:1] if len(products) == 1 else False

    @api.onchange('driver_user_id')
    def _onchange_driver_user_id(self):
        employee = self.driver_user_id.sudo().employee_id
        vehicle, _problem = self.env['stock.picking']._water_assigned_vehicle(employee)
        self.vehicle_id = vehicle if employee else False

    @api.model_create_multi
    def create(self, vals_list):
        Rate = self.env['ksw.water.rate'].sudo()
        Picking = self.env['stock.picking']
        for vals in vals_list:
            if not vals.get('product_id') and vals.get('partner_id'):
                # A read-only one-product field is left out of the browser's
                # payload; create() is the guard that matters (gotcha #145).
                products = Rate.search([('partner_id', '=', vals['partner_id'])]).product_id
                if len(products) == 1:
                    vals['product_id'] = products.id
            if vals.get('driver_user_id') and not vals.get('vehicle_id'):
                employee = self.env['res.users'].browse(vals['driver_user_id']).sudo().employee_id
                vehicle = Picking._water_assigned_vehicle(employee)[0]
                if vehicle:
                    vals['vehicle_id'] = vehicle.id
        records = super().create(vals_list)
        records._check_branch_authority()
        return records

    def write(self, vals):
        if not self.env.su and _ORDER_FIELDS & set(vals):
            if any(d.state != 'draft' for d in self):
                raise UserError(_(
                    'A dispatch can only be changed while it is a draft. Send it '
                    'back to draft first (only possible before the driver has '
                    'confirmed it), or cancel it and make a new one.'))
            self._check_branch_authority()
        res = super().write(vals)
        if 'picking_type_id' in vals:
            self._check_branch_authority()
        return res

    def unlink(self):
        if any(d.state not in ('draft', 'cancelled') for d in self):
            raise UserError(_('Only a draft or cancelled dispatch can be deleted.'))
        return super().unlink()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_dispatch(self):
        """Send it to the driver. Every rule is checked here, server side --
        the form's pickers are a convenience, not the guard."""
        self._check_branch_authority()
        Picking = self.env['stock.picking'].sudo()
        Rate = self.env['ksw.water.rate'].sudo()
        for dispatch in self:
            if dispatch.state != 'draft':
                raise UserError(_('Only a draft can be dispatched.'))
            branch = dispatch.picking_type_id.sudo()
            if branch.x_issue_mode != 'dispatch':
                raise UserError(_(
                    '%s is set so that drivers issue their own deliveries. '
                    'Switch the branch to "Dispatcher dispatches, driver '
                    'confirms" first (Water Delivery ▸ Configuration ▸ '
                    'Branches).', branch.display_name))
            if dispatch.partner_id not in dispatch.allowed_partner_ids:
                raise UserError(_(
                    '%(client)s is not one of %(branch)s\'s priced clients.',
                    client=dispatch.partner_id.display_name,
                    branch=branch.display_name))
            if not Rate._rate_for(dispatch.partner_id, dispatch.product_id):
                raise UserError(_(
                    'There is no agreed rate for %(product)s with %(client)s.',
                    product=dispatch.product_id.display_name,
                    client=dispatch.partner_id.display_name))
            if dispatch.entered_qty <= 0:
                raise UserError(_('The quantity must be greater than zero.'))
            if not dispatch.driver_user_id:
                raise UserError(_('Choose the driver.'))
            if dispatch.driver_user_id not in dispatch.allowed_driver_user_ids:
                raise UserError(_(
                    '%(driver)s is not driving a tanker of %(branch)s.',
                    driver=dispatch.driver_user_id.name, branch=branch.display_name))
            vehicle, problem = Picking._water_assigned_vehicle(dispatch.driver_id)
            if problem:
                raise UserError(problem)
            if Picking._water_picking_type_for(vehicle) != branch:
                raise UserError(_(
                    'Tanker %(tanker)s belongs to another branch.',
                    tanker=vehicle.display_name))
            if dispatch.entered_uom == 'trip' and not vehicle.x_capacity_m3:
                raise UserError(_(
                    'Tanker %s has no trip volume, so the quantity cannot be '
                    'given in trips. Enter it in m³, or set the trip volume.',
                    vehicle.display_name))
            values = {
                'state': 'dispatched',
                'vehicle_id': vehicle.id,
                'dispatcher_id': self.env.uid,
                'dispatched_on': fields.Datetime.now(),
            }
            if dispatch.name in (False, 'New', _('New')):
                values['name'] = self.env['ir.sequence'].sudo().next_by_code(
                    'ksw.water.dispatch') or _('New')
            dispatch.sudo().write(values)

    def action_draft(self):
        """Take it back from the driver -- only while he has not confirmed it."""
        self._check_branch_authority()
        for dispatch in self:
            if dispatch.state != 'dispatched' or dispatch.capture_id:
                raise UserError(_(
                    'Only a dispatch the driver has not yet confirmed can go '
                    'back to draft.'))
        self.sudo().write({'state': 'draft'})

    def action_cancel(self):
        self._check_branch_authority()
        for dispatch in self:
            if dispatch.state not in ('draft', 'dispatched') or dispatch.capture_id:
                raise UserError(_(
                    'This dispatch has already been confirmed by the driver. '
                    'A delivery that happened is reviewed by the supervisor '
                    'on the delivery itself, not cancelled here.'))
        self.sudo().write({'state': 'cancelled'})

    # ------------------------------------------------------------------
    # The driver's side, called from ksw.water.capture
    # ------------------------------------------------------------------
    def _water_capture_problem(self, driver):
        """Why this dispatch cannot be confirmed by `driver` now, or None."""
        self.ensure_one()
        if self.driver_id != driver:
            return _('%(dispatch)s was given to %(driver)s before this '
                     'confirmation arrived.', dispatch=self.name,
                     driver=self.driver_id.name or _('another driver'))
        if self.state != 'dispatched':
            return _('%(dispatch)s is %(state)s, not waiting for delivery.',
                     dispatch=self.name,
                     state=dict(self._fields['state']._description_selection(
                         self.env))[self.state])
        if self.capture_id and self.capture_id.state != 'rejected':
            return _('%(dispatch)s was already confirmed (%(capture)s).',
                     dispatch=self.name, capture=self.capture_id.uuid)
        return None

    def _water_capture_values(self):
        """What the note is made of: the dispatcher's order, never the
        phone's copy of it."""
        self.ensure_one()
        return {
            'partner_id': self.partner_id.id,
            'product_id': self.product_id.id,
            'entered_qty': self.entered_qty,
            'entered_uom': self.entered_uom,
            'vehicle_id': self.vehicle_id.id,
            'note': self.note or False,
            'off_route': False,
        }

    def _water_on_capture_issued(self, capture):
        for dispatch in self:
            dispatch.write({
                'state': 'delivered',
                'capture_id': capture.id,
                'picking_id': capture.picking_id.id,
                'delivered_on': capture.captured_at,
            })
            dispatch.message_post(body=Markup(
                '<strong>%(title)s</strong> %(note)s'
            ) % {
                'title': _('Delivered by the driver:'),
                'note': capture.picking_id.name or '',
            })

    def _water_on_capture_rejected(self, capture):
        """The supervisor says it did not happen: the order is open again."""
        for dispatch in self.filtered(lambda d: d.capture_id == capture):
            dispatch.write({
                'state': 'dispatched', 'capture_id': False,
                'picking_id': False, 'delivered_on': False,
            })
            dispatch.message_post(body=Markup('%(text)s') % {
                'text': _('The driver\'s delivery was rejected by the '
                          'supervisor; the dispatch is open again.'),
            })

    @api.model
    def _water_open_for_driver(self, driver):
        """What the driver's phone shows under My Dispatches."""
        return self.sudo().search([
            ('driver_id', '=', driver.id),
            ('state', '=', 'dispatched'),
            '|', ('capture_id', '=', False), ('capture_id.state', '=', 'rejected'),
        ], order='dispatched_on, id')
