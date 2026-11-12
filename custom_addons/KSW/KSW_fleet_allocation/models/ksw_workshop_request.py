from markupsafe import Markup

from odoo import _, models


class KswWorkshopRequest(models.Model):
    _inherit = 'ksw.workshop.request'

    def action_complete(self):
        res = super().action_complete()
        # A transfer decided while the vehicle was in use waited for this
        # checkup: it starts now.
        Vehicle = self.env['ksw.fleet.vehicle'].sudo()
        for request in self.sudo():
            vehicle = request.vehicle_id
            if not vehicle or vehicle.x_allocation_state != 'holding' or not vehicle.x_reserved_department_id:
                continue
            target = vehicle.x_reserved_department_id
            line = vehicle.x_reserved_addition_line_id
            move = Vehicle.browse(vehicle.id).with_context(ksw_fleet_batch=False)._ksw_fleet_start_transfer(
                target, vehicle.x_reserved_note, addition_line=line or None,
                source=vehicle.x_reserved_source or 'internal')
            for fleet_request in move.release_line_id.request_id | move.addition_line_id.request_id:
                fleet_request._ksw_fleet_post(Markup(_(
                    'Checkup %(w)s completed: <b>%(v)s</b> is now on its way to <b>%(d)s</b>.'))
                    % {'w': request.name, 'v': vehicle.name, 'd': target.name})
                fleet_request._ksw_fleet_sync_state()
        return res
