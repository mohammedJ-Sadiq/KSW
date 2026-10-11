import logging
from statistics import median

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# The worst fix a driver may pin a client with. The pin becomes the centre of
# the enforcement radius (300 m by default), so a coarse cell-tower fix here
# would hold every later note at that site for a dispatcher. Outdoors, away
# from the cab, a phone reaches this in well under a minute even offline.
PIN_MAX_ACCURACY_M = 50.0


class ResPartner(models.Model):
    _inherit = 'res.partner'

    def action_set_location_from_deliveries(self):
        """Learn a client's location from the notes already issued there.

        No client in the database has coordinates, so the location rule cannot
        be enforced until they do -- and nobody is going to survey 312 sites.
        The drivers record a position every time they issue a note, so after a
        few deliveries the answer is already in the data.

        The **median** of the captured points, not the mean: one note issued
        from the depot by mistake drags a mean across town, and the whole point
        of the figure is to be the site rather than the average of everywhere a
        driver has been.
        """
        Picking = self.env['stock.picking'].sudo()
        updated = self.browse()
        for partner in self:
            points = Picking.search([
                ('x_is_water_delivery', '=', True),
                ('partner_id', '=', partner.id),
                ('x_gps_latitude', '!=', 0.0),
                ('x_gps_longitude', '!=', 0.0),
            ])
            if not points:
                continue
            partner.sudo().write({
                'partner_latitude': median(points.mapped('x_gps_latitude')),
                'partner_longitude': median(points.mapped('x_gps_longitude')),
            })
            updated |= partner

        if not updated:
            raise UserError(_(
                'None of the selected clients has a delivery note carrying a '
                'recorded position yet. Locations are learned from the notes '
                'the drivers issue on site.'
            ))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Client Locations'),
                'message': _('%s client(s) located from their delivery notes.',
                             len(updated)),
                'type': 'success',
            },
        }

    # ------------------------------------------------------------------
    # A driver pinning a client from the site
    # ------------------------------------------------------------------
    @api.model
    def _water_receive_pins(self, pins, employee):
        """Take the phone's queued "this is where the client is" pins.

        Under the enforce rule a client with no approved location cannot
        receive a note at all, so this is the only way a new site gets one: the
        driver sends a pin from the gate, and it becomes a
        `ksw.water.location.request` for a dispatcher to approve. It never
        writes the client's location itself.

        Answered per uuid, like `/water/sync`, so the phone knows which it may
        drop. A pin that can never succeed (poor fix, client outside his
        scope, already located) is settled too: resending it changes nothing.
        """
        Picking = self.env['stock.picking'].sudo()
        vehicle = Picking._water_assigned_vehicle(employee)[0]
        picking_type = Picking._water_picking_type_for(vehicle)
        _normal, rated = Picking._water_client_scope(employee, picking_type)

        results = []
        for pin in (pins or []):
            uuid = (pin or {}).get('uuid')
            try:
                with self.env.cr.savepoint():
                    result = self._water_receive_pin(pin, employee, rated)
                results.append(dict(result, uuid=uuid, settled=True))
            except Exception as error:  # noqa: BLE001 - one bad pin, not the batch
                _logger.exception('KSW_water_delivery: pin %s could not be received', uuid)
                results.append({'uuid': uuid, 'status': 'error',
                                'message': str(error), 'settled': False})
        return results

    @api.model
    def _water_receive_pin(self, pin, employee, allowed_partners):
        partner = self.sudo().browse(int(pin.get('partner_id') or 0)).exists()
        if not partner or partner not in allowed_partners:
            return {'status': 'refused', 'message': _(
                'This client is not one you deliver to.')}
        try:
            latitude = float(pin.get('latitude') or 0.0)
            longitude = float(pin.get('longitude') or 0.0)
            accuracy = float(pin.get('accuracy') or 0.0)
        except (TypeError, ValueError):
            return {'status': 'refused', 'message': _('The position was unreadable.')}
        if (not (latitude or longitude)
                or not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0)):
            return {'status': 'refused', 'message': _('The position was unreadable.')}
        if not accuracy or accuracy > PIN_MAX_ACCURACY_M:
            return {'status': 'refused', 'message': _(
                'The position was too rough (±%(accuracy)d m; at most %(limit)d m '
                'is accepted).', accuracy=int(accuracy), limit=int(PIN_MAX_ACCURACY_M))}

        Request = self.env['ksw.water.location.request'].sudo()
        uuid = pin.get('uuid')
        # The phone resends anything it has no answer for; the second copy
        # must find the first rather than queue a duplicate for review.
        existing = Request.search([('uuid', '=', uuid)], limit=1) if uuid else Request
        if existing:
            return {'status': existing.state, 'name': partner.display_name}
        if partner.partner_latitude or partner.partner_longitude:
            return {'status': 'already', 'name': partner.display_name,
                    'message': _('%s already has a location.', partner.display_name)}

        Request.create({
            'uuid': uuid,
            'partner_id': partner.id,
            'employee_id': employee.id,
            'user_id': employee.sudo().user_id.id or self.env.uid,
            'phone_latitude': latitude, 'phone_longitude': longitude,
            'latitude': latitude, 'longitude': longitude,
            'accuracy_m': accuracy,
            'captured_at': fields.Datetime.to_datetime(pin.get('captured_at')) or False,
        })
        return {'status': 'pending', 'name': partner.display_name}
