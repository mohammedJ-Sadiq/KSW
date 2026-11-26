from markupsafe import Markup

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
                'workshop_request_id': workshop_request.id, 'workshop_id': workshop_request.workshop_id.id,
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
                # The workshop that held it hands it over; a vehicle straight
                # from the pool goes to the receiving department's workshop.
                'workshop_id': (hand_in.workshop_id
                                or self.env['ksw.workshop']._for_department(move.to_department_id)).id,
                'origin': move.addition_line_id.request_id.name or move.display_name,
                'previous_employee_id': hand_in.previous_employee_id.id,
                'deliverer_name': hand_in.workshop_official_name or False,
                'workshop_official_name': hand_in.workshop_official_name or False,
                'inspection_datetime': hand_in.inspection_datetime,
            })
            if hand_in:
                form._load_inspection_items(source=hand_in)
            form._ksw_open_handover_request(move)
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
    """Custody follows the workshop request (user, 2026-11-26):
    - an ordinary visit takes the vehicle from its driver while In Progress
      and gives it back to him when Done (knowledge only, no HR-12);
    - a release (hand-in HR-12) or a driver change frees the driver the
      moment the workshop starts, so he can be given a vehicle at once; the
      vehicle does not come back to him;
    - a handover (receipt HR-12) or a driver change gives the vehicle to the
      new driver when the request is Done (after the signed HR-12)."""
    _inherit = 'ksw.workshop.request'

    x_custodian_on_hold_id = fields.Many2one('hr.employee', string='Driver Until Back', readonly=True, copy=False,
                                             help='Ordinary visit: the driver the vehicle goes back to when done.')

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

    def _ksw_live_forms(self):
        return self.sudo().x_custody_request_ids.filtered(lambda f: f.state not in ('cancelled', 'refused'))

    def action_start(self):
        res = super().action_start()
        for request in self.sudo():
            request._ksw_custody_on_start()
        return res

    def _ksw_custody_on_start(self):
        vehicle = self.vehicle_id.sudo()
        if not vehicle or not vehicle.x_custodian_id:
            return
        forms = self._ksw_live_forms()
        leaving = forms.filtered(lambda f: f.kind in ('hand_in', 'driver_change'))
        if leaving:
            # For good: the supervisor may ask for another vehicle for him now.
            who = vehicle.x_custodian_id
            vehicle._ksw_set_custodian(self.env['hr.employee'], source=leaving[0].name,
                                       note=_('Released when the workshop started %(r)s: %(e)s is free for '
                                              'another vehicle.', r=self.name, e=who.name))
        elif not forms:
            # An ordinary visit: only while it lasts.
            self.x_custodian_on_hold_id = vehicle.x_custodian_id
            vehicle._ksw_set_custodian(self.env['hr.employee'], source=self.name,
                                       note=_('In the workshop; back to %s when done.', vehicle.x_custodian_id.name))

    def _ksw_custody_on_complete(self):
        vehicle = self.vehicle_id.sudo()
        if not vehicle:
            return
        forms = self._ksw_live_forms()
        arriving = forms.filtered(lambda f: f.kind in ('receive', 'driver_change') and f.new_employee_id)[:1]
        if arriving:
            if vehicle.x_custodian_id != arriving.new_employee_id:
                vehicle._ksw_set_custodian(arriving.new_employee_id, source=arriving.name,
                                           note=_('Handed over when %s was completed.', self.name))
            vehicle.write({'driver_id': arriving.new_employee_id.id})
            move = arriving.move_id.sudo()
            if arriving.kind == 'receive' and move.state == 'in_transfer':
                move._ksw_fleet_complete({})
        elif not forms and self.x_custodian_on_hold_id and not vehicle.x_custodian_id \
                and not ('x_is_stopped' in vehicle._fields and vehicle.x_is_stopped):
            vehicle.with_context(ksw_custody_unchecked=True)._ksw_set_custodian(
                self.x_custodian_on_hold_id, source=self.name, note=_('Back from the workshop.'))
        self.x_custodian_on_hold_id = False

    def action_complete(self):
        # The checkup closes only with its HR-12 approved and the signed copy
        # filed: completing it is what lets the vehicle out of the workshop.
        pending = self.sudo().x_custody_request_ids.filtered(
            lambda f: f.state not in ('signed', 'cancelled', 'refused'))
        if pending:
            raise UserError(_(
                'File the signed handover form first (%s): the workshop and HR complete it, it is '
                'printed, signed, scanned and marked as signed.', ', '.join(pending.mapped('name'))))
        res = super().action_complete()
        for request in self.sudo():
            request._ksw_custody_on_complete()
        return res

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
    # Driven vehicles he answers for now (a tedder is its head's, not his).
    x_vehicles_held_ids = fields.Many2many('ksw.fleet.vehicle', compute='_compute_vehicles_held', compute_sudo=True,
                                           string='Vehicles He Holds', groups='hr.group_hr_user')
    x_vehicles_held_count = fields.Integer(compute='_compute_vehicles_held', compute_sudo=True,
                                           string='Vehicles Held', groups='hr.group_hr_user')
    x_holds_several = fields.Boolean(compute='_compute_vehicles_held', compute_sudo=True,
                                     search='_search_holds_several', string='Holds Several Vehicles',
                                     groups='hr.group_hr_user')
    x_over_vehicle_limit = fields.Boolean(compute='_compute_vehicles_held', compute_sudo=True,
                                          search='_search_over_vehicle_limit', string='More Vehicles Than Allowed',
                                          groups='hr.group_hr_user')
    x_vehicles_pending_my_action = fields.Boolean(compute='_compute_vehicles_pending', search='_search_vehicles_pending',
                                                  string='Vehicles Waiting for Me', groups='hr.group_hr_user')

    def _ksw_announce_several(self, held):
        """Custody HR and his department's manager are told, on his record."""
        self.ensure_one()
        group = self.env.ref('KSW_vehicle_custody.group_custody_hr', raise_if_not_found=False)
        technical = self.env.ref('base.user_admin', raise_if_not_found=False) | self.env.ref('base.user_root')
        users = (group.user_ids - technical) if group else self.env['res.users']
        managers = held.mapped('x_department_id.manager_id.user_id') if 'x_department_id' in held._fields \
            else self.env['res.users']
        users |= managers | self.department_id.manager_id.user_id
        limit = max(self.x_vehicle_limit, 1)
        body = Markup(_('<b>%(e)s now holds %(n)s vehicles</b>: %(v)s. Allowed: %(l)s.%(over)s')) % {
            'e': self.name, 'n': len(held), 'v': ', '.join(held.mapped('name')), 'l': limit,
            'over': Markup(_(' <b>Over the limit: hand one over.</b>')) if len(held) > limit else ''}
        self.message_post(body=body, partner_ids=users.partner_id.ids, subtype_xmlid='mail.mt_comment')

    def _held_counts(self):
        rows = self.env['ksw.fleet.vehicle'].sudo()._read_group(
            [('x_custodian_id', '!=', False), ('vehicle_type', '!=', 'tank')], ['x_custodian_id'], ['__count'])
        return {emp.id: n for emp, n in rows}

    def _compute_vehicles_held(self):
        Vehicle = self.env['ksw.fleet.vehicle'].sudo()
        for emp in self:
            held = Vehicle.search([('x_custodian_id', '=', emp.id), ('vehicle_type', '!=', 'tank')]) if emp.id \
                else Vehicle
            emp.x_vehicles_held_ids = held
            emp.x_vehicles_held_count = len(held)
            emp.x_holds_several = len(held) > 1
            emp.x_over_vehicle_limit = len(held) > max(emp.sudo().x_vehicle_limit, 1)

    @staticmethod
    def _bool_search(operator, value):
        if operator in ('in', 'not in'):
            return (operator == 'in') == any(value)
        if operator in ('=', '!='):
            return (operator == '=') == bool(value)
        return None

    def _search_holds_several(self, operator, value):
        wanted = self._bool_search(operator, value)
        if wanted is None:
            return NotImplemented
        ids = [eid for eid, n in self._held_counts().items() if n > 1]
        return [('id', 'in' if wanted else 'not in', ids)]

    def _search_over_vehicle_limit(self, operator, value):
        wanted = self._bool_search(operator, value)
        if wanted is None:
            return NotImplemented
        counts = self._held_counts()
        limits = {e.id: max(e.x_vehicle_limit, 1) for e in self.sudo().browse(list(counts))}
        ids = [eid for eid, n in counts.items() if n > limits.get(eid, 1)]
        return [('id', 'in' if wanted else 'not in', ids)]

    @api.depends_context('uid')
    def _compute_vehicles_pending(self):
        mine = self.env.su or self.env.user.has_group('KSW_vehicle_custody.group_custody_hr')
        for emp in self:
            emp.x_vehicles_pending_my_action = mine and emp.x_over_vehicle_limit

    def _search_vehicles_pending(self, operator, value):
        wanted = self._bool_search(operator, value)
        if wanted is None:
            return NotImplemented
        if not (self.env.su or self.env.user.has_group('KSW_vehicle_custody.group_custody_hr')):
            return [('id', '=', False)] if wanted else []
        return self._search_over_vehicle_limit('=', wanted)


class KswFleetVehicleCustodyAlerts(models.Model):
    """Drivers holding more than one vehicle: allowed by exception
    (x_vehicle_limit), but always shown and announced (user, 2026-11-26)."""
    _inherit = 'ksw.fleet.vehicle'

    x_custodian_vehicle_count = fields.Integer(compute='_compute_custodian_vehicle_count', compute_sudo=True,
                                               string='Vehicles His Driver Holds')

    @api.depends('x_custodian_id')
    def _compute_custodian_vehicle_count(self):
        for vehicle in self:
            emp = vehicle.x_custodian_id
            vehicle.x_custodian_vehicle_count = self.sudo().search_count(
                [('x_custodian_id', '=', emp.id), ('vehicle_type', '!=', 'tank')]) if emp else 0

    def _ksw_set_custodian(self, employee, source=False, date=False, note=False):
        res = super()._ksw_set_custodian(employee, source=source, date=date, note=note)
        if employee:
            held = self.sudo().search([('x_custodian_id', '=', employee.id), ('vehicle_type', '!=', 'tank')])
            if len(held) > 1:
                employee.sudo()._ksw_announce_several(held)
        return res


class HrDepartment(models.Model):
    _inherit = 'hr.department'

    x_driver_change_workshop = fields.Boolean(
        string='Driver Change Through Workshop',
        help='A driver change between two of this department\'s drivers goes through HR (the new '
             'driver\'s licence) and a workshop checkup, like a vehicle received from another '
             'department. Unticked, the supervisor\'s confirmation completes it and HR is told.')

    def _ksw_driver_change_through_workshop(self):
        """The setting of the vehicle-holding department this one belongs to."""
        self.ensure_one()
        holder = self._ksw_fleet_holder() or self.sudo()
        return bool(holder.x_driver_change_workshop)


class KswWorkshopPart(models.Model):
    _inherit = 'ksw.workshop.part'

    x_routine_service = fields.Boolean(
        string='Routine Service (Oil / Filter)',
        help='Oil and filters changed at every service. Left out of the maintenance history on the '
             'vehicle handover form (HR-12), which lists the repairs and parts that matter.')
