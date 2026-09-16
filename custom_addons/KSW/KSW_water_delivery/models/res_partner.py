from statistics import median

from odoo import _, models
from odoo.exceptions import UserError


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
