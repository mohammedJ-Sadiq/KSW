from odoo import _, api, fields, models
from odoo.exceptions import UserError


class KswFleetRequest(models.Model):
    """A request is an intent (release these / we need these) and only the
    GM decides it. HR-12 belongs to executing his decision, so the forms are
    opened when he decides, never before."""
    _inherit = 'ksw.fleet.request'

    x_custody_request_ids = fields.One2many('ksw.vehicle.custody.request', 'fleet_request_id',
                                            string='Handover Forms')

    def action_cancel(self):
        res = super().action_cancel()
        self.sudo().x_custody_request_ids._ksw_cancel(_('The release request was cancelled.'))
        return res


class KswFleetReleaseLine(models.Model):
    _inherit = 'ksw.fleet.release.line'

    x_custody_request_id = fields.Many2one('ksw.vehicle.custody.request', string='HR-12', readonly=True,
                                           copy=False)
    x_custody_state = fields.Selection(related='x_custody_request_id.state', string='HR-12 Status')


class KswFleetVehicle(models.Model):
    _inherit = 'ksw.fleet.vehicle'

    def _ksw_fleet_after_holding(self, move, workshop_request, release_line):
        """The GM sent the vehicle to the workshop (to hold, or on its way to
        a department that asked): its custodian hands it in. The workshop
        inspects it while HR collects its documents, in parallel."""
        res = super()._ksw_fleet_after_holding(move, workshop_request, release_line)
        Custody = self.env['ksw.vehicle.custody.request'].sudo()
        for vehicle in self:
            current = vehicle._ksw_fleet_current_driver()
            request = release_line.request_id if release_line else move.addition_line_id.request_id
            form = Custody.create({
                'kind': 'hand_in', 'state': 'workshop', 'vehicle_id': vehicle.id,
                'department_id': vehicle.x_department_id.id,
                'fleet_request_id': release_line.request_id.id if release_line else False,
                'workshop_request_id': workshop_request.id,
                'origin': request.name or move.display_name,
                'previous_employee_id': current.id, 'deliverer_name': current.name or False,
                'supervisor_id': release_line.request_id.user_id.employee_id.id if release_line else False,
                'workshop_reason': (release_line.note if release_line else False) or workshop_request.description,
            })
            if release_line:
                release_line.sudo().x_custody_request_id = form
            form._sync_activities()
        return res

    def _ksw_fleet_after_transfer_started(self, move):
        """The vehicle is on its way to a department that asked for it: that
        department names the new driver, HR checks him (licence before
        handover), then the workshop hands it over. That handover is the
        arrival. A vehicle out of the workshop carries its inspection."""
        res = super()._ksw_fleet_after_transfer_started(move)
        if self.env.context.get('ksw_fleet_with_driver'):
            # The vehicle moves together with its custodian: nobody new to name.
            return res
        Custody = self.env['ksw.vehicle.custody.request'].sudo()
        for vehicle in self:
            hand_in = Custody.search([('vehicle_id', '=', vehicle.id), ('kind', '=', 'hand_in'),
                                      ('state', 'in', ('done', 'signed'))], limit=1)
            form = Custody.create({
                'kind': 'receive', 'state': 'draft', 'vehicle_id': vehicle.id,
                'department_id': move.to_department_id.id, 'move_id': move.id,
                'origin': move.addition_line_id.request_id.name or move.display_name,
                'previous_employee_id': hand_in.previous_employee_id.id,
                'deliverer_name': hand_in.workshop_official_name or False,
                'workshop_official_name': hand_in.workshop_official_name or False,
                'inspection_datetime': hand_in.inspection_datetime,
            })
            if hand_in:
                form._load_inspection_items(source=hand_in)
            form._sync_activities()
        return res


class KswFleetMove(models.Model):
    _inherit = 'ksw.fleet.move'

    x_custody_request_ids = fields.One2many('ksw.vehicle.custody.request', 'move_id', string='Handover Forms')

    @api.model
    def _ksw_receipt_by_department(self):
        # The workshop's handover to the new driver (HR-12) is the arrival.
        return False

    def _ksw_fleet_on_cancel(self):
        res = super()._ksw_fleet_on_cancel()
        self.sudo().x_custody_request_ids._ksw_cancel(_('The transfer was cancelled by the GM.'))
        return res

    def action_open_custody_form(self):
        self.ensure_one()
        form = self.sudo().x_custody_request_ids.filtered(lambda f: f.state != 'cancelled')[:1]
        if not form:
            raise UserError(_('This transfer has no handover form.'))
        return {'type': 'ir.actions.act_window', 'name': form.name,
                'res_model': 'ksw.vehicle.custody.request', 'res_id': form.id, 'view_mode': 'form'}


class KswWorkshopRequest(models.Model):
    _inherit = 'ksw.workshop.request'

    x_custody_request_ids = fields.One2many('ksw.vehicle.custody.request', 'workshop_request_id',
                                            string='Handover Forms')
    # sudo: shown to workshop staff on the request form; a failed o2m read
    # would take the whole form down.
    x_custody_count = fields.Integer(compute='_compute_custody', compute_sudo=True)
    x_custody_pending = fields.Boolean(compute='_compute_custody', compute_sudo=True)

    @api.depends('x_custody_request_ids.state')
    def _compute_custody(self):
        for request in self:
            forms = request.x_custody_request_ids.filtered(lambda f: f.state not in ('cancelled', 'refused'))
            request.x_custody_count = len(forms)
            request.x_custody_pending = bool(forms.filtered(lambda f: f.state != 'signed'))

    def action_complete(self):
        # The checkup closes only with its HR-12 approved and the signed copy
        # filed: completing it is what lets the vehicle out of the workshop.
        pending = self.sudo().x_custody_request_ids.filtered(
            lambda f: f.state not in ('signed', 'cancelled', 'refused'))
        if pending:
            raise UserError(_(
                'File the signed handover form first (%s): the workshop and HR complete it, it is '
                'printed, signed, scanned and marked as signed.', ', '.join(pending.mapped('name'))))
        return super().action_complete()

    def action_open_custody_forms(self):
        self.ensure_one()
        forms = self.sudo().x_custody_request_ids.filtered(lambda f: f.state != 'cancelled')
        action = self.env['ir.actions.act_window']._for_xml_id('KSW_vehicle_custody.action_ksw_vehicle_custody')
        action['context'] = {}
        if len(forms) == 1:
            action.update({'res_id': forms.id, 'view_mode': 'form', 'views': [(False, 'form')]})
        else:
            action['domain'] = [('workshop_request_id', '=', self.id)]
        return action


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    x_custody_history_ids = fields.One2many('ksw.fleet.vehicle.custody', 'employee_id',
                                            string='Vehicle Custody', groups='hr.group_hr_user')
