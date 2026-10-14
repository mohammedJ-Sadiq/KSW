from odoo import _, api, fields, models
from odoo.exceptions import UserError


class KswWorkshopRequest(models.Model):
    _inherit = 'ksw.workshop.request'

    # The base domain, plus: only trucks the requester answers for. The trailing
    # ('id', '=', vehicle_id) keeps a request's existing vehicle readable after
    # its driver moves to another supervisor's team.
    vehicle_id = fields.Many2one(
        domain="[('client_id', '=', client_id), ('vehicle_type', '=', vehicle_type), "
               "'|', ('state', '=', 'confirmed'), ('id', '=', vehicle_id), "
               "'|', ('x_workshop_in_scope', '=', True), ('id', '=', vehicle_id)]",
    )
    # A snapshot, not a related: who was attached to the truck WHEN the request
    # was raised. The truck's driver changes with the next BAS update, and a
    # related would then flag (or un-flag) a months-old request. Written only
    # by create()/write() from the vehicle, never by the client.
    x_attached_driver_id = fields.Many2one(
        'hr.employee', string="Vehicle's Driver", readonly=True, copy=False,
        help='The driver attached to this vehicle when the request was raised '
             '(from BAS, or the default driver when BAS has none).')
    x_driver_mismatch = fields.Boolean(
        string='Driver Differs', compute='_compute_driver_mismatch', store=True,
        help='The driver on this request is not the driver attached to the vehicle.')

    @api.depends('driver_id', 'x_attached_driver_id', 'vehicle_id', 'is_cash_customer')
    def _compute_driver_mismatch(self):
        for request in self:
            request.x_driver_mismatch = bool(
                not request.is_cash_customer and request.vehicle_id
                and request.x_attached_driver_id and request.driver_id
                and request.driver_id != request.x_attached_driver_id)

    @api.onchange('vehicle_id')
    def _onchange_vehicle_id(self):
        super()._onchange_vehicle_id()
        if not self._driver_follows_vehicle():
            return
        for request in self:
            attached = request.vehicle_id.sudo().x_attached_driver_id
            request.x_attached_driver_id = attached
            if attached:
                request.driver_id = attached

    @api.model
    def _pick_on_behalf(self, managers, vehicle_id):
        """The first assisted requester (own manager first) whose trucks
        include this one. A client's vehicle is in everyone's scope, so it
        falls to the own manager like any truck they share."""
        if vehicle_id:
            Vehicle = self.env['ksw.fleet.vehicle']
            for manager in managers:
                if Vehicle.with_user(manager).search_count(
                        [('id', '=', vehicle_id), ('x_workshop_in_scope', '=', True)]):
                    return manager
        return super()._pick_on_behalf(managers, vehicle_id)

    def action_submit(self):
        """The phone's Submit button. Saving IS submitting: create() already
        notified the workshop, and the client saves a new record before it
        runs any button. This only says so, because on a phone the save is a
        small cloud icon nobody reads as "send"."""
        self.ensure_one()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Request submitted'),
                'message': _('%s was sent to the workshop.', self.name),
                'type': 'success',
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    def _check_vehicle_in_scope(self, vehicle_id):
        if not vehicle_id:
            return
        vehicle = self.env['ksw.fleet.vehicle'].browse(vehicle_id)
        # A draft is a truck the requester has just registered on this form
        # (KSW_fleet's assignment request); it cannot be in anyone's group yet.
        if vehicle.sudo().state == 'draft':
            return
        if not vehicle.x_workshop_in_scope:
            raise UserError(_(
                '%s is not one of your vehicles: it is not in your tanker groups '
                'and none of your team drives it.', vehicle.sudo().display_name))

    @api.model_create_multi
    def create(self, vals_list):
        Vehicle = self.env['ksw.fleet.vehicle'].sudo()
        for vals in vals_list:
            vals.pop('x_attached_driver_id', None)
            if vals.get('vehicle_id'):
                if not vals.get('x_imported'):
                    self._check_vehicle_in_scope(vals['vehicle_id'])
                # Switched off with the fill-in: no snapshot means no
                # "driver changed" warning against a driver nobody suggested.
                if not self._driver_follows_vehicle():
                    continue
                attached = Vehicle.browse(vals['vehicle_id']).x_attached_driver_id
                vals['x_attached_driver_id'] = attached.id
                if attached and not vals.get('driver_id'):
                    vals['driver_id'] = attached.id
        return super().create(vals_list)

    def write(self, vals):
        vals = dict(vals)
        vals.pop('x_attached_driver_id', None)
        if 'vehicle_id' in vals and self.filtered(lambda r: r.vehicle_id.id != vals['vehicle_id']):
            if not self.env.su:
                self._check_vehicle_in_scope(vals['vehicle_id'])
            vehicle = self.env['ksw.fleet.vehicle'].sudo().browse(vals['vehicle_id'])
            vals['x_attached_driver_id'] = vehicle.x_attached_driver_id.id \
                if self._driver_follows_vehicle() else False
        return super().write(vals)
