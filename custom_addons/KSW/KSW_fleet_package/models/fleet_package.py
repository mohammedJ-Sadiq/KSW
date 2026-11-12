from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class KswFleetReleaseLine(models.Model):
    _inherit = 'ksw.fleet.release.line'

    x_with_driver = fields.Boolean(string='With Driver',
                                   help='The vehicle moves together with the employee who answers for it: '
                                        'no workshop, no handover form, and he is transferred too.')
    # Who goes with it, fixed when the request is submitted.
    x_driver_id = fields.Many2one('hr.employee', string='Driver Going With It', readonly=True, copy=False)
    x_employee_transfer_id = fields.Many2one('ksw.employee.transfer', string='Employee Transfer',
                                             readonly=True, copy=False)


class KswFleetRequest(models.Model):
    _inherit = 'ksw.fleet.request'

    def _ksw_custody_lines(self):
        return super()._ksw_custody_lines().filtered(lambda l: not l.x_with_driver)

    def action_submit(self):
        for line in self.release_line_ids.filtered('x_with_driver'):
            custodian = line.vehicle_id.sudo().x_custodian_id
            if not custodian:
                raise UserError(_('%s has no custodian to move with it. Release it without '
                                  'its driver, or have HR record its custodian first.', line.vehicle_id.name))
            line.sudo().x_driver_id = custodian
        return super().action_submit()

    def action_hr_send_to_gm(self):
        """HR's approval covers the drivers going with their vehicles: their
        licences and documents are checked here, there is no HR-12."""
        res = super().action_hr_send_to_gm()
        for request in self.sudo():
            drivers = request.release_line_ids.filtered('x_with_driver').x_driver_id
            if drivers:
                request._ksw_fleet_post(Markup(_('HR checked the documents of the drivers moving with '
                                                 'their vehicles: %s.')) % ', '.join(drivers.mapped('name')))
        return res


class KswFleetReleaseDecisionWizard(models.TransientModel):
    _inherit = 'ksw.fleet.release.decision.wizard'

    def action_confirm(self):
        self.ensure_one()
        package = self.release_line_ids.filtered('x_with_driver')
        if package and self.decision == 'holding':
            raise UserError(_('A vehicle released with its driver is transferred with him or kept, '
                              'not sent to the workshop: %s.', ', '.join(package.vehicle_id.mapped('name'))))
        if not package or self.decision != 'transfer':
            return super().action_confirm()
        if not self.env['ksw.fleet.request']._ksw_fleet_is_gm():
            raise UserError(_('Only the GM decides a release.'))
        if not self.target_department_id:
            raise UserError(_('Choose the department that receives them.'))
        others = self.release_line_ids - package
        package.sudo()._ksw_package_transfer(self.target_department_id, self.note)
        if others:
            self.release_line_ids = others
            return super().action_confirm()
        return {'type': 'ir.actions.act_window_close'}


class KswFleetReleaseLinePackage(models.Model):
    _inherit = 'ksw.fleet.release.line'

    def _ksw_package_transfer(self, target, note=False):
        """Vehicle and driver move now: the department changes at once (no
        workshop, no receipt), the custodian keeps the vehicle, and he is
        transferred to the same department from today."""
        Transfer = self.env['ksw.employee.transfer'].sudo()
        for line in self:
            if line.decision != 'pending' or line.request_id.state != 'submitted':
                raise UserError(_('%s has already been decided.', line.vehicle_id.name))
            if line.department_id == target:
                raise UserError(_('%s already belongs to that department.', line.vehicle_id.name))
            vehicle = line.vehicle_id.with_context(ksw_fleet_direct=True, ksw_fleet_with_driver=True,
                                                   ksw_fleet_batch=True)
            move = vehicle._ksw_fleet_start_transfer(target, note)
            move._ksw_fleet_complete({})
            line.write({'decision': 'transfer', 'target_department_id': target.id, 'decision_note': note,
                        'decided_by_id': self.env.user.id, 'decided_date': fields.Datetime.now()})
            driver = line.x_driver_id
            if driver.department_id == target:
                continue
            transfer = Transfer.create({
                'employee_id': driver.id, 'to_department_id': target.id,
                'effective_date': fields.Date.context_today(self), 'state': 'approved',
                'with_vehicles': True, 'origin': line.request_id.name,
                'reason': _('Moves with %(v)s (%(r)s).', v=vehicle.name, r=line.request_id.name),
            })
            transfer._apply()
            line.x_employee_transfer_id = transfer
        for request in self.request_id:
            lines = self.filtered(lambda l: l.request_id == request)
            hr = self.env.ref('KSW_vehicle_custody.group_custody_hr').sudo().all_user_ids
            accounting = self.env.ref('KSW_vehicle_custody.group_custody_accounting').sudo().all_user_ids
            request._ksw_fleet_post(Markup(_(
                '<b>%(v)s</b> moved to <b>%(d)s</b> together with their drivers (%(e)s), effective '
                'today. For your action if any: delegation (HR), financial matters (Accounting).')) % {
                'v': ', '.join(lines.vehicle_id.mapped('name')), 'd': target.name,
                'e': ', '.join(lines.x_driver_id.mapped('name'))},
                partners=(hr | accounting).filtered(lambda u: u.active and not u.share).partner_id)
            request._ksw_fleet_sync_state()


class ResUsers(models.Model):
    _inherit = 'res.users'

    def _ksw_transfer_department_ids(self):
        """A department's vehicle users may also ask to move its people."""
        ids = set(super()._ksw_transfer_department_ids())
        return list(ids | set(self._ksw_fleet_department_ids()))
