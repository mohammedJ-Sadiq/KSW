from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class KswFleetReleaseLine(models.Model):
    _inherit = 'ksw.fleet.release.line'

    # Who goes with it, fixed when the request is submitted.
    x_driver_id = fields.Many2one('hr.employee', string='Driver Going With It', readonly=True, copy=False)
    x_employee_transfer_id = fields.Many2one('ksw.employee.transfer', string='Employee Transfer',
                                             readonly=True, copy=False)

    def _ksw_execute_transfer(self, addition_line, note=False):
        """A vehicle released with its driver fills a need that accepts a
        driver: both move now. The department changes at once (no workshop,
        no handover form), the custodian keeps the vehicle, and he is
        transferred to the same department from today."""
        package = self.filtered('x_with_driver')
        moves = super(KswFleetReleaseLine, self - package)._ksw_execute_transfer(addition_line, note) \
            if self - package else self.env['ksw.fleet.move']
        Transfer = self.env['ksw.employee.transfer'].sudo()
        target = addition_line.department_id
        for line in package.sudo():
            vehicle = line.vehicle_id.with_context(ksw_fleet_direct=True, ksw_fleet_with_driver=True,
                                                   ksw_fleet_batch=True)
            move = vehicle._ksw_fleet_start_transfer(target, note, addition_line=addition_line)
            move._ksw_fleet_complete({})
            moves |= move
            driver = line.x_driver_id
            if driver.department_id != target:
                transfer = Transfer.create({
                    'employee_id': driver.id, 'to_department_id': target.id,
                    'effective_date': fields.Date.context_today(self), 'state': 'approved',
                    'with_vehicles': True, 'origin': line.request_id.name,
                    'reason': _('Moves with %(v)s (%(r)s).', v=vehicle.name, r=line.request_id.name),
                })
                transfer._apply()
                line.x_employee_transfer_id = transfer
        if package:
            hr = self.env.ref('KSW_vehicle_custody.group_custody_hr').sudo().all_user_ids
            accounting = self.env.ref('KSW_vehicle_custody.group_custody_accounting').sudo().all_user_ids
            for request in package.request_id | addition_line.request_id:
                request._ksw_fleet_post(Markup(_(
                    '<b>%(v)s</b> moved to <b>%(d)s</b> together with their drivers (%(e)s), effective '
                    'today. For your action if any: delegation and documents (HR), financial matters '
                    '(Accounting).')) % {
                    'v': ', '.join(package.vehicle_id.mapped('name')), 'd': target.name,
                    'e': ', '.join(package.x_driver_id.mapped('name'))},
                    partners=(hr | accounting).filtered(lambda u: u.active and not u.share).partner_id)
        return moves


class KswFleetRequest(models.Model):
    _inherit = 'ksw.fleet.request'

    def action_submit(self):
        for line in self.release_line_ids.filtered('x_with_driver'):
            custodian = line.vehicle_id.sudo().x_custodian_id
            if not custodian:
                raise UserError(_('%s has no custodian to move with it. Release it without '
                                  'its driver, or have HR record its custodian first.', line.vehicle_id.name))
            line.sudo().x_driver_id = custodian
        return super().action_submit()


class ResUsers(models.Model):
    _inherit = 'res.users'

    def _ksw_transfer_department_ids(self):
        """A department's vehicle users may also ask to move its people."""
        ids = set(super()._ksw_transfer_department_ids())
        return list(ids | set(self._ksw_fleet_department_ids()))
