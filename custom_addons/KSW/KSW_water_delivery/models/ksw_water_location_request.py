from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

DISPATCHER_GROUP = 'KSW_water_delivery.group_water_dispatcher'


class KswWaterLocationRequest(models.Model):
    """A driver saying "this is where the client is", waiting for the office.

    Under the enforce rule a client with no location cannot receive a note at
    all, so the location is the gate -- and a gate a driver could open by
    himself is not one. The pin he sends from the site is therefore a request:
    a dispatcher looks at it on the map, corrects it if he must, and only the
    approved figure reaches the client. From then on every note for that
    client is checked against it.
    """
    _name = 'ksw.water.location.request'
    _description = 'Client Location Request'
    _inherit = ['mail.thread']
    _order = 'state_sequence, received_at desc, id desc'

    uuid = fields.Char(required=True, readonly=True, copy=False, index=True)
    partner_id = fields.Many2one(
        'res.partner', string='Client', required=True, readonly=True, index=True)
    employee_id = fields.Many2one('hr.employee', string='Sent by', readonly=True)
    user_id = fields.Many2one('res.users', string='Sent by (user)', readonly=True)

    # What the phone recorded, kept as sent. The two below are what will be
    # approved -- a dispatcher may move the pin onto the gate before approving,
    # and the original reading must survive that edit.
    phone_latitude = fields.Float(digits=(10, 7), readonly=True)
    phone_longitude = fields.Float(digits=(10, 7), readonly=True)
    latitude = fields.Float(digits=(10, 7), required=True, tracking=True)
    longitude = fields.Float(digits=(10, 7), required=True, tracking=True)
    accuracy_m = fields.Float(string='Accuracy (m)', readonly=True)
    captured_at = fields.Datetime(readonly=True, help="The phone's clock at the client.")
    received_at = fields.Datetime(readonly=True, default=fields.Datetime.now)
    map_url = fields.Char(string='Map', compute='_compute_map_url')

    state = fields.Selection(
        [('pending', 'Waiting for Approval'),
         ('approved', 'Approved'),
         ('rejected', 'Rejected')],
        default='pending', required=True, readonly=True, tracking=True, index=True)
    state_sequence = fields.Integer(compute='_compute_state_sequence', store=True)
    reviewed_by = fields.Many2one('res.users', readonly=True)
    reviewed_on = fields.Datetime(readonly=True)
    reject_reason = fields.Char(tracking=True)

    _uuid_unique = models.Constraint('UNIQUE(uuid)', 'This location was already received.')

    @api.depends('state')
    def _compute_state_sequence(self):
        order = {'pending': 0, 'approved': 1, 'rejected': 2}
        for request in self:
            request.state_sequence = order.get(request.state, 9)

    @api.depends('latitude', 'longitude')
    def _compute_map_url(self):
        for request in self:
            request.map_url = (
                'https://www.google.com/maps/search/?api=1&query=%.7f,%.7f'
                % (request.latitude, request.longitude)
                if request.latitude or request.longitude else False)

    def _compute_display_name(self):
        for request in self:
            request.display_name = request.partner_id.display_name or _('Location')

    # ------------------------------------------------------------------
    # Review
    # ------------------------------------------------------------------
    def _check_reviewer(self):
        if self.env.su:
            return
        user = self.env.user
        if not user.has_group(DISPATCHER_GROUP):
            raise UserError(_('Only a supervisor can approve or reject a client location.'))
        # A dispatcher is also a driver (the group implies it), so he could
        # otherwise pin a client and wave his own pin through. The approval is
        # only worth something if it is somebody else's.
        if not user.has_group('base.group_system') and self.filtered(
                lambda r: r.user_id == user):
            raise UserError(_(
                'You sent this location yourself, so another supervisor has to '
                'approve it.'))

    def action_approve(self):
        self._check_reviewer()
        for request in self:
            if request.state != 'pending':
                raise UserError(_('Only a location waiting for approval can be approved.'))
            if not (request.latitude or request.longitude) or not (
                    -90.0 <= request.latitude <= 90.0
                    and -180.0 <= request.longitude <= 180.0):
                raise UserError(_('The location is not a valid position.'))
            partner = request.partner_id.sudo()
            partner.write({
                'partner_latitude': request.latitude,
                'partner_longitude': request.longitude,
                'date_localization': fields.Date.context_today(request),
            })
            request.write({
                'state': 'approved',
                'reviewed_by': self.env.user.id,
                'reviewed_on': fields.Datetime.now(),
            })
            # The client has one location; the other readings for it are
            # answered rather than left waiting forever.
            others = self.search([
                ('partner_id', '=', partner.id), ('state', '=', 'pending'),
                ('id', '!=', request.id),
            ])
            others.write({
                'state': 'rejected',
                'reviewed_by': self.env.user.id,
                'reviewed_on': fields.Datetime.now(),
                'reject_reason': _('Another location was approved for this client.'),
            })
            moved = (abs(request.latitude - request.phone_latitude) > 1e-7
                     or abs(request.longitude - request.phone_longitude) > 1e-7)
            partner.message_post(
                body=Markup(
                    '📍 Location approved by %(reviewer)s: %(lat).6f, %(lon).6f<br/>'
                    'Sent from the site by %(driver)s (±%(accuracy)d m)%(moved)s'
                ) % {
                    'reviewer': self.env.user.name,
                    'lat': request.latitude, 'lon': request.longitude,
                    'driver': request.employee_id.name or request.user_id.name or '',
                    'accuracy': int(request.accuracy_m),
                    'moved': _(', adjusted before approval') if moved else '',
                },
                subtype_xmlid='mail.mt_note',
            )
        return True

    def action_reject(self):
        self._check_reviewer()
        for request in self:
            if request.state != 'pending':
                raise UserError(_('Only a location waiting for approval can be rejected.'))
            if not request.reject_reason:
                raise UserError(_('Write the reason for rejecting it first, so the driver knows.'))
            request.write({
                'state': 'rejected',
                'reviewed_by': self.env.user.id,
                'reviewed_on': fields.Datetime.now(),
            })
        return True

    # ------------------------------------------------------------------
    # From the phone
    # ------------------------------------------------------------------
    @api.model
    def _pending_partner_ids(self, partner_ids):
        return set(self.sudo().search([
            ('partner_id', 'in', list(partner_ids)), ('state', '=', 'pending'),
        ]).partner_id.ids)
