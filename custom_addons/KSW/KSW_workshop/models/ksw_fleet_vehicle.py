from odoo import _, api, fields, models


class KswFleetVehicle(models.Model):
    """Workshop-side extension of the fleet vehicle.

    Lives in KSW_workshop, not KSW_fleet: the dependency runs
    KSW_workshop -> KSW_fleet, so the vehicle module must not know about
    workshop requests.
    """
    _inherit = 'ksw.fleet.vehicle'

    workshop_request_count = fields.Integer(
        string='Workshop Visits', compute='_compute_workshop_request_count',
    )

    def _compute_workshop_request_count(self):
        # read_group in one query rather than a search_count per record —
        # the vehicle list is opened with 300+ rows.
        counts = self.env['ksw.workshop.request']._read_group(
            [('vehicle_id', 'in', self.ids)], ['vehicle_id'], ['__count'],
        )
        count_map = {vehicle.id: count for vehicle, count in counts}
        for vehicle in self:
            vehicle.workshop_request_count = count_map.get(vehicle.id, 0)

    def action_view_workshop_requests(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Workshop Visits'),
            'res_model': 'ksw.workshop.request',
            'view_mode': 'list,form,pivot,graph',
            'domain': [('vehicle_id', '=', self.id)],
            # no default vehicle_id: the request form derives it from the
            # client/vehicle-type cascade, and forcing it here would fight
            # _compute_vehicle_id.
            'context': {'search_default_groupby_request_type': 1},
        }
