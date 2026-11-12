from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

HR_GROUP = 'KSW_vehicle_custody.group_custody_hr'


class KswFleetRequest(models.Model):
    """A release now goes Supervisor → HR → GM (one request, one GM
    decision): HR checks each vehicle's HR-12 before the GM sees it."""
    _inherit = 'ksw.fleet.request'

    state = fields.Selection(selection_add=[('hr_review', 'HR Review'), ('submitted',)],
                             ondelete={'hr_review': 'set default'})
    x_custody_request_ids = fields.One2many('ksw.vehicle.custody.request', 'fleet_request_id',
                                            string='Handover Forms')
    x_is_hr = fields.Boolean(compute='_compute_is_hr')

    @api.depends_context('uid')
    def _compute_is_hr(self):
        is_hr = self.env['ksw.vehicle.custody.request']._is_hr()
        for request in self:
            request.x_is_hr = is_hr

    def _ksw_custody_lines(self):
        """The release lines that hand a vehicle in (HR-12). The package
        bridge leaves out vehicles moving together with their driver."""
        return self.release_line_ids

    def action_submit(self):
        res = super().action_submit()
        releases = self.sudo().filtered(lambda r: r.request_type == 'release')
        if releases:
            releases.write({'state': 'hr_review'})
            Custody = self.env['ksw.vehicle.custody.request'].sudo()
            for request in releases:
                for line in request._ksw_custody_lines():
                    vehicle = line.vehicle_id
                    current = vehicle._ksw_fleet_current_driver()
                    line.x_custody_request_id = Custody.create({
                        'kind': 'hand_in', 'state': 'hr', 'vehicle_id': vehicle.id,
                        'department_id': request.department_id.id,
                        'fleet_request_id': request.id, 'origin': request.name,
                        'previous_employee_id': current.id,
                        'supervisor_id': request.user_id.employee_id.id,
                        'workshop_reason': line.note or request.reason,
                        'deliverer_name': current.name or False,
                    })
            releases._ksw_fleet_sync_activities()
        return res

    def action_hr_send_to_gm(self):
        """HR approves the request's handover forms and passes it to the GM."""
        if not self.env['ksw.vehicle.custody.request']._is_hr():
            raise UserError(_('Only HR reviews a release.'))
        for request in self:
            if request.state != 'hr_review':
                raise UserError(_('%s is not waiting for HR.', request.name))
            request._ksw_check_hr_review()
        for request in self.sudo():
            forms = request.x_custody_request_ids.filtered(lambda f: f.state == 'hr')
            # As the reviewer himself: the forms check HR's rights and stamp his name.
            forms.with_env(self.env).action_hr_approve()
            request.state = 'submitted'
            request._ksw_fleet_post(Markup(_('Reviewed by HR (%s); waiting for the GM.')) % self.env.user.name)
        self._ksw_fleet_sync_activities()
        return True

    def _ksw_check_hr_review(self):
        """Hook: what HR must have done before the GM sees the request."""
        return True

    def action_cancel(self):
        res = super().action_cancel()
        self.sudo().x_custody_request_ids._ksw_cancel(_('The release request was cancelled.'))
        return res

    def _ksw_fleet_sync_activities(self):
        super()._ksw_fleet_sync_activities()
        todo = self.env.ref('KSW_vehicle_custody.mail_activity_custody')
        hr_users = self.env.ref(HR_GROUP).sudo().all_user_ids.filtered(lambda u: u.active and not u.share)
        for request in self.sudo().exists():
            wanted = {u.id: _('HR review: %s', request.name) for u in hr_users} \
                if request.state == 'hr_review' else {}
            for activity in request.activity_ids.filtered(lambda a: a.activity_type_id == todo):
                if wanted.get(activity.user_id.id) == activity.summary:
                    wanted.pop(activity.user_id.id)
                else:
                    activity.unlink()
            for user_id, summary in wanted.items():
                request.activity_schedule(activity_type_id=todo.id, user_id=user_id, summary=summary)


class KswFleetReleaseLine(models.Model):
    _inherit = 'ksw.fleet.release.line'

    x_custody_request_id = fields.Many2one('ksw.vehicle.custody.request', string='HR-12', readonly=True,
                                           copy=False)
    x_custody_state = fields.Selection(related='x_custody_request_id.state', string='HR-12 Status')

    def write(self, vals):
        res = super().write(vals)
        if vals.get('decision') == 'keep':
            # The GM kept it: the hand-in it was prepared for does not happen.
            self.sudo().x_custody_request_id._ksw_cancel(_('The GM decided to keep the vehicle.'))
        return res


class KswFleetVehicle(models.Model):
    _inherit = 'ksw.fleet.vehicle'

    def _ksw_fleet_after_holding(self, move, workshop_request, release_line):
        """The vehicle goes to the workshop: its hand-in form follows the GM
        decision there. One taken by the GM without a release gets its own,
        HR still checks it, then the workshop."""
        res = super()._ksw_fleet_after_holding(move, workshop_request, release_line)
        Custody = self.env['ksw.vehicle.custody.request'].sudo()
        for vehicle in self:
            form = release_line.sudo().x_custody_request_id if release_line else Custody
            if not form or form.state in ('cancelled', 'refused'):
                current = vehicle._ksw_fleet_current_driver()
                form = Custody.create({
                    'kind': 'hand_in', 'state': 'hr', 'vehicle_id': vehicle.id,
                    'department_id': vehicle.x_department_id.id, 'origin': move.display_name,
                    'previous_employee_id': current.id, 'deliverer_name': current.name or False,
                    'workshop_reason': workshop_request.description,
                })
                if release_line:
                    release_line.sudo().x_custody_request_id = form
            form._gm_decided_to_workshop(workshop_request)
        return res

    def _ksw_fleet_after_transfer_started(self, move):
        """The receiving department names the new driver on its own form; a
        vehicle out of the workshop carries the workshop's inspection."""
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
                'origin': move.display_name,
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

    def _ksw_fleet_check_receivable(self):
        res = super()._ksw_fleet_check_receivable()
        waiting = self.sudo().filtered(lambda m: m.x_custody_request_ids.filtered(
            lambda f: f.state not in ('done', 'signed', 'cancelled')))
        if waiting:
            raise UserError(_(
                'The new driver\'s handover form (HR-12) must be approved first (named by the '
                'department, checked by HR, handed over by the workshop): %s.',
                ', '.join(waiting.vehicle_id.mapped('name'))))
        return res

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
                'File the signed handover form first (%s): the workshop completes it, it is '
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
