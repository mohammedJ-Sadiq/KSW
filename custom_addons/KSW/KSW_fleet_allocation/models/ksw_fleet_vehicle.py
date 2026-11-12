import logging

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class KswFleetVehicle(models.Model):
    _inherit = 'ksw.fleet.vehicle'

    # A client's truck is the client's: only the company's own fleet is
    # allocated to departments.
    x_is_own_fleet = fields.Boolean(
        string='Own Fleet', compute='_compute_is_own_fleet', store=True, index=True)

    # Set by the allocation flow (transfers, holding) and by an administrator
    # loading the starting list; write() refuses anyone else and logs every
    # change as a movement. Not readonly= in Python, or the importer would
    # leave it out of the columns it offers.
    x_department_id = fields.Many2one(
        'hr.department', string='Department', index=True, ondelete='restrict',
        domain=[('x_fleet_holder', '=', True)])
    x_allocation_state = fields.Selection([
        ('unassigned', 'Unassigned'),
        ('assigned', 'Assigned'),
        ('in_transfer', 'In Transfer'),
        ('holding', 'Holding (Workshop)'),
    ], string='Allocation', default='unassigned', required=True, copy=False, index=True)
    x_move_ids = fields.One2many('ksw.fleet.move', 'vehicle_id', string='Movements')
    # A transfer decided while the vehicle was in use waits for its workshop
    # checkup; these hold where it goes when the checkup completes.
    x_reserved_department_id = fields.Many2one('hr.department', string='Reserved For', readonly=True,
                                               copy=False, index=True)
    x_reserved_addition_line_id = fields.Many2one('ksw.fleet.addition.line', readonly=True, copy=False)
    x_reserved_release_line_id = fields.Many2one('ksw.fleet.release.line', readonly=True, copy=False)
    x_reserved_source = fields.Selection([('internal', 'Internal'), ('external', 'External')],
                                         readonly=True, copy=False)
    x_reserved_note = fields.Text(readonly=True, copy=False)
    x_pending_move_id = fields.Many2one(
        'ksw.fleet.move', compute='_compute_pending_move_id', string='Transfer in Progress')
    # Only so x_is_released recomputes when a release is raised or decided.
    x_release_line_ids = fields.One2many('ksw.fleet.release.line', 'vehicle_id', string='Releases')
    x_is_released = fields.Boolean(
        string='Released', compute='_compute_is_released', search='_search_is_released',
        help='Its department has asked to give it up and the GM has not decided yet.')
    x_release_request_id = fields.Many2one(
        'ksw.fleet.request', compute='_compute_is_released', string='Released By')

    # A vehicle sent to the workshop is only given out again once its
    # checkup is finished: that is what sending it there was for.
    x_checkup_request_id = fields.Many2one(
        'ksw.workshop.request', compute='_compute_checkup', string='Checkup Request')
    # Text, not the request itself: the GM may not read workshop requests.
    x_checkup_label = fields.Char(string='Checkup', compute='_compute_checkup')
    x_checkup_open = fields.Boolean(
        string='Checkup Open', compute='_compute_checkup', search='_search_checkup_open')

    def _ksw_fleet_open_checkups(self, every_vehicle=False):
        """{vehicle id: workshop request} for these held vehicles (every
        held vehicle with every_vehicle) whose latest checkup is not finished.
        An empty recordset means none, never all."""
        if not self and not every_vehicle:
            return {}
        domain = [('kind', '=', 'holding'), ('vehicle_id.x_allocation_state', '=', 'holding')]
        if not every_vehicle:
            domain.append(('vehicle_id', 'in', self.ids))
        moves = self.env['ksw.fleet.move'].sudo().search(domain, order='id desc')
        latest = {}
        for move in moves:
            latest.setdefault(move.vehicle_id.id, move.workshop_request_id)
        return {vid: request for vid, request in latest.items()
                if request and request.state not in self._CHECKUP_DONE_STATES}

    _CHECKUP_DONE_STATES = ('completed', 'rejected')

    @api.depends('x_allocation_state', 'x_move_ids.workshop_request_id.state')
    def _compute_checkup(self):
        held = self.filtered(lambda v: v.x_allocation_state == 'holding')
        open_checkups = held._ksw_fleet_open_checkups() if held else {}
        for vehicle in self:
            last = vehicle.x_move_ids.filtered(lambda m: m.kind == 'holding').sorted('id', reverse=True)[:1] \
                if vehicle.x_allocation_state == 'holding' else vehicle.x_move_ids.browse()
            vehicle.x_checkup_request_id = last.workshop_request_id
            request = last.workshop_request_id.sudo()
            vehicle.x_checkup_label = '%s (%s)' % (
                request.name, dict(request._fields['state']._description_selection(self.env))[request.state]
            ) if request else False
            vehicle.x_checkup_open = vehicle.id in open_checkups

    def _search_checkup_open(self, operator, value):
        # Odoo 19 hands a boolean condition over as in/not in (gotcha #24).
        if operator in ('in', 'not in'):
            wanted = (operator == 'in') == any(value)
        elif operator in ('=', '!='):
            wanted = (operator == '=') == bool(value)
        else:
            return NotImplemented
        ids = list(self.browse()._ksw_fleet_open_checkups(every_vehicle=True))
        return [('id', 'in' if wanted else 'not in', ids)]

    # "My department's vehicles". Vehicles stay readable company-wide (the
    # workshop and water-delivery pickers need them), so the Vehicle
    # Allocation menus narrow by this instead of a record rule.
    x_in_my_scope = fields.Boolean(
        string='My Departments', compute='_compute_in_my_scope', search='_search_in_my_scope')

    @api.model
    def _ksw_fleet_sees_all(self):
        return self.env['ksw.fleet.request']._ksw_fleet_is_gm() \
            or self.env.user.has_group('base.group_system')

    @api.depends_context('uid')
    @api.depends('x_department_id')
    def _compute_in_my_scope(self):
        sees_all = self._ksw_fleet_sees_all()
        scope = set(self.env.user._ksw_fleet_department_ids())
        for vehicle in self:
            vehicle.x_in_my_scope = sees_all or vehicle.x_department_id.id in scope

    def _search_in_my_scope(self, operator, value):
        # Odoo 19 hands a boolean condition over as in/not in (gotcha #24).
        if operator in ('in', 'not in'):
            wanted = (operator == 'in') == any(value)
        elif operator in ('=', '!='):
            wanted = (operator == '=') == bool(value)
        else:
            return NotImplemented
        if self._ksw_fleet_sees_all():
            return [] if wanted else [('id', '=', False)]
        ids = self.env.user._ksw_fleet_department_ids()
        if wanted:
            return [('x_department_id', 'in', ids)]
        return ['|', ('x_department_id', '=', False), ('x_department_id', 'not in', ids)]

    @api.depends('client_id')
    def _compute_is_own_fleet(self):
        own = set(self.env['res.company'].sudo().search([]).partner_id.ids)
        for vehicle in self:
            vehicle.x_is_own_fleet = vehicle.client_id.id in own

    @api.depends('x_move_ids.state')
    def _compute_pending_move_id(self):
        for vehicle in self:
            vehicle.x_pending_move_id = vehicle.x_move_ids.filtered(
                lambda m: m.state == 'in_transfer')[:1]

    def _open_release_lines(self):
        return self.env['ksw.fleet.release.line'].sudo().search([
            ('vehicle_id', 'in', self.ids), ('decision', '=', 'pending'),
            ('request_id.state', '=', 'submitted')])

    @api.depends('x_release_line_ids.decision', 'x_release_line_ids.request_id.state')
    def _compute_is_released(self):
        lines = {line.vehicle_id.id: line for line in self._open_release_lines()}
        for vehicle in self:
            line = lines.get(vehicle.id)
            vehicle.x_is_released = bool(line)
            vehicle.x_release_request_id = line.request_id if line else False

    def _search_is_released(self, operator, value):
        # Odoo 19 hands a boolean condition over as in/not in (gotcha #24).
        if operator in ('in', 'not in'):
            wanted = (operator == 'in') == any(value)
        elif operator in ('=', '!='):
            wanted = (operator == '=') == bool(value)
        else:
            return NotImplemented
        ids = self.env['ksw.fleet.release.line'].sudo().search([
            ('decision', '=', 'pending'), ('request_id.state', '=', 'submitted'),
        ]).vehicle_id.ids
        return [('id', 'in' if wanted else 'not in', ids)]

    # ------------------------------------------------------------------
    # Direct department changes (the starting list) are an administrator's
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and any(vals.get('x_department_id') for vals in vals_list) \
                and not self.env.user.has_group('base.group_system'):
            raise UserError(_('Only the system administrator sets a vehicle\'s department directly.'))
        for vals in vals_list:
            if vals.get('x_department_id'):
                vals['x_allocation_state'] = 'assigned'
        vehicles = super().create(vals_list)
        for vehicle in vehicles.filtered('x_department_id'):
            vehicle._ksw_fleet_log_manual(self.env['hr.department'], vehicle.x_department_id, 'initial')
        return vehicles

    def write(self, vals):
        if self.env.context.get('ksw_fleet_allocation') or not (
                {'x_department_id', 'x_allocation_state'} & set(vals)):
            return super().write(vals)
        if not self.env.su and not self.env.user.has_group('base.group_system'):
            raise UserError(_(
                'A vehicle changes department through a release or addition request '
                'decided by the GM. Only the system administrator sets it directly.'))
        if 'x_allocation_state' in vals and 'x_department_id' not in vals:
            raise UserError(_('The allocation status follows the vehicle\'s movements; '
                              'it cannot be set by hand.'))
        new_department = self.env['hr.department'].browse(vals['x_department_id'] or [])
        changed = self.filtered(lambda v: v.x_department_id != new_department)
        if changed.filtered(lambda v: v.x_allocation_state == 'in_transfer'):
            raise UserError(_(
                'These vehicles are being transferred: %s. Confirm or cancel the transfer first.',
                ', '.join(changed.filtered(lambda v: v.x_allocation_state == 'in_transfer').mapped('name'))))
        old = {vehicle.id: vehicle.x_department_id for vehicle in changed}
        vals = dict(vals)
        vals.pop('x_allocation_state', None)
        res = super().write(vals)
        for vehicle in changed:
            vehicle.with_context(ksw_fleet_allocation=True).write({
                'x_allocation_state': 'assigned' if new_department else 'unassigned'})
            vehicle._ksw_fleet_log_manual(old[vehicle.id], new_department,
                                          'manual' if old[vehicle.id] else 'initial')
        return res

    def _ksw_fleet_log_manual(self, from_department, to_department, kind):
        self.env['ksw.fleet.move'].sudo().create({
            'vehicle_id': self.id, 'kind': kind, 'state': 'done',
            'from_department_id': from_department.id, 'to_department_id': to_department.id,
            'decided_by_id': self.env.user.id, 'received_date': fields.Datetime.now(),
            'received_by_id': self.env.user.id,
        })

    # ------------------------------------------------------------------
    # The two things a GM decision does to a vehicle
    # ------------------------------------------------------------------
    def _ksw_fleet_check_movable(self):
        for vehicle in self:
            if not vehicle.x_is_own_fleet:
                raise UserError(_('%s belongs to a client, not to the company fleet.', vehicle.name))
            if vehicle.x_allocation_state == 'in_transfer':
                raise UserError(_('%s is already being transferred.', vehicle.name))
        open_checkups = self.filtered(lambda v: v.x_allocation_state == 'holding')._ksw_fleet_open_checkups()
        if open_checkups:
            raise UserError(_(
                'Still at the workshop: %s. A held vehicle is given out once its checkup is completed.',
                ', '.join('%s (%s)' % (self.browse(vid).name, request.name)
                          for vid, request in open_checkups.items())))

    def _ksw_fleet_current_driver(self):
        """Who answers for it now: its custodian, else its operating driver."""
        self.ensure_one()
        return self.sudo().x_custodian_id or self.sudo().driver_id

    def _ksw_fleet_start_transfer(self, target, note=False, addition_line=None, source='internal'):
        """GM decision: move this vehicle to `target`.

        A vehicle still in use in a department goes through the workshop
        first: it is sent for a checkup with the transfer reserved, and the
        transfer starts by itself when the checkup completes (only a vehicle
        moving together with its driver skips this, ksw_fleet_direct). A
        vehicle already in the pool is checked, so it moves at once, In
        Transfer until the receiving side confirms."""
        self.ensure_one()
        self._ksw_fleet_check_movable()
        if self.x_department_id == target:
            raise UserError(_('%(v)s already belongs to %(d)s.', v=self.name, d=target.name))
        if self.x_allocation_state == 'assigned' and not self.env.context.get('ksw_fleet_direct'):
            return self._ksw_fleet_send_to_holding(
                _('Checkup before transfer to %s', target.name), note,
                reserve=target, reserve_addition_line=addition_line, reserve_source=source)
        release_line = self._open_release_lines()[:1] or self.sudo().x_reserved_release_line_id
        move = self.env['ksw.fleet.move'].sudo().create({
            'vehicle_id': self.id, 'kind': 'transfer', 'state': 'in_transfer',
            'from_department_id': self.x_department_id.id, 'to_department_id': target.id,
            'from_state': self.x_allocation_state,
            'decided_by_id': self.env.user.id, 'note': note,
            'release_line_id': release_line.id,
            'addition_line_id': addition_line.id if addition_line else False,
            'source': source,
        })
        if release_line and release_line.decision == 'pending':
            release_line.write({
                'decision': 'transfer', 'target_department_id': target.id,
                'decision_note': note, 'decided_by_id': self.env.user.id,
                'decided_date': fields.Datetime.now(),
            })
            if not self.env.context.get('ksw_fleet_batch'):
                release_line.request_id._ksw_fleet_post(Markup(
                    _('<b>%(v)s</b> will be transferred to <b>%(d)s</b>.%(n)s'))
                    % {'v': self.name, 'd': target.name, 'n': _note(note)})
        self.sudo().with_context(ksw_fleet_allocation=True).write({
            'x_allocation_state': 'in_transfer', 'x_reserved_department_id': False,
            'x_reserved_addition_line_id': False, 'x_reserved_release_line_id': False})
        anchor = addition_line.request_id if addition_line else release_line.request_id
        if anchor and not self.env.context.get('ksw_fleet_batch'):
            anchor._ksw_fleet_post(Markup(
                _('<b>%(v)s</b> is on its way to <b>%(d)s</b>. Confirm on its arrival under '
                'Vehicle Allocation ▸ Transfers to Receive.%(n)s'))
                % {'v': self.name, 'd': target.name, 'n': _note(note)})
        self._ksw_fleet_after_transfer_started(move)
        return move

    def _ksw_fleet_after_transfer_started(self, move):
        """Hook: the custody module opens the receiving form here."""
        return True

    def _ksw_fleet_after_holding(self, move, workshop_request, release_line):
        """Hook: the custody module opens the hand-in form here."""
        return True

    def _ksw_fleet_send_to_holding(self, description, note=False, reserve=None,
                                   reserve_addition_line=None, reserve_source='internal'):
        """GM decision: the vehicle leaves its department and goes to the
        workshop for a checkup. Without `reserve` it then waits in the Holding
        pool; with it, the transfer to that department starts by itself when
        the checkup completes (ksw.workshop.request.action_complete)."""
        self.ensure_one()
        self._ksw_fleet_check_movable()
        employee = self.env.user.employee_id
        if not employee:
            raise UserError(_('Your user has no employee record, so the workshop request '
                              'cannot name who raised it. Ask the administrator to link one.'))
        release_line = self._open_release_lines()[:1]
        department = self.x_department_id
        # The workshop needs a driver: who answers for the truck, else the
        # GM's employee record, who hands it over.
        driver = self._ksw_fleet_current_driver() or employee
        workshop_request = self.env['ksw.workshop.request'].sudo().create({
            'employee_id': employee.id,
            'client_id': self.client_id.id,
            'vehicle_type': self.vehicle_type,
            'vehicle_id': self.id,
            'driver_id': driver.id,
            'workshop_id': self.env['ksw.workshop']._for_department(department).id,
            'description': description or _('Checkup before reallocation'),
        })
        move = self.env['ksw.fleet.move'].sudo().create({
            'vehicle_id': self.id, 'kind': 'holding', 'state': 'done',
            'from_department_id': department.id, 'from_state': self.x_allocation_state,
            'decided_by_id': self.env.user.id, 'note': note,
            'received_date': fields.Datetime.now(), 'received_by_id': self.env.user.id,
            'release_line_id': release_line.id, 'workshop_request_id': workshop_request.id,
            # A reserved vehicle already counts against the request it answers.
            'addition_line_id': reserve_addition_line.id if reserve_addition_line else False,
            'source': reserve_source,
            'reserved_department_id': reserve.id if reserve else False,
        })
        if release_line:
            release_line.write({
                'decision': 'transfer' if reserve else 'holding',
                'target_department_id': reserve.id if reserve else False,
                'decision_note': note,
                'decided_by_id': self.env.user.id, 'decided_date': fields.Datetime.now(),
            })
        if release_line and not self.env.context.get('ksw_fleet_batch'):
            release_line.request_id._ksw_fleet_post(Markup(
                _('<b>%(v)s</b> was sent to the workshop for a checkup (%(w)s) and is now in '
                'the Holding pool.%(n)s')) % {'v': self.name, 'w': workshop_request.name, 'n': _note(note)})
        self._ksw_fleet_after_holding(move, workshop_request, release_line)
        self.sudo().with_context(ksw_fleet_allocation=True).write({
            'x_reserved_department_id': reserve.id if reserve else False,
            'x_reserved_addition_line_id': reserve_addition_line.id if reserve_addition_line else False,
            'x_reserved_release_line_id': release_line.id if reserve else False,
            'x_reserved_source': reserve_source if reserve else False,
            'x_reserved_note': note if reserve else False,
        })
        self.sudo().with_context(ksw_fleet_allocation=True).write({
            'x_department_id': False, 'x_allocation_state': 'holding'})
        return move

    def action_ksw_fleet_release_selected(self):
        """Vehicles list ▸ Release Selected: a draft release request for the
        ticked vehicles, opened for the reason to be written."""
        vehicles = self.sudo()
        if vehicles.filtered(lambda v: not v.x_is_own_fleet or v.x_allocation_state != 'assigned'):
            raise UserError(_('Only vehicles assigned to a department can be released: %s.', ', '.join(
                vehicles.filtered(lambda v: not v.x_is_own_fleet or v.x_allocation_state != 'assigned').mapped('name'))))
        holders = self.env['hr.department']
        for vehicle in vehicles:
            holders |= vehicle.x_department_id._ksw_fleet_holder()
        if len(holders) != 1:
            raise UserError(_('Tick vehicles of one department at a time (these belong to %s).',
                              ', '.join(holders.mapped('name'))))
        if not self._ksw_fleet_sees_all() and holders.id not in self.env.user._ksw_fleet_department_ids():
            raise UserError(_('You do not answer for the vehicles of %s.', holders.name))
        released = vehicles.filtered('x_is_released')
        if released:
            raise UserError(_('Already on a release request: %s.', ', '.join(released.mapped('name'))))
        # The reason is required, so the request opens unsaved and prefilled.
        return {
            'type': 'ir.actions.act_window',
            'name': _('Release Vehicles'),
            'res_model': 'ksw.fleet.request',
            'view_mode': 'form',
            'target': 'current',
            'context': {
                'default_request_type': 'release',
                'default_department_id': holders.id,
                'default_release_line_ids': [(0, 0, {'vehicle_id': v.id}) for v in vehicles],
            },
        }

    # ------------------------------------------------------------------
    # Starting list
    # ------------------------------------------------------------------
    @api.model
    def _ksw_fleet_seed_from_drivers(self):
        """Give every unassigned own-fleet vehicle the vehicle-holding
        department of its driver (the BAS driver when known). Vehicles whose
        driver is in no such department are left for the administrator's
        list. Returns the vehicles assigned."""
        vehicles = self.sudo().search([('x_is_own_fleet', '=', True), ('x_department_id', '=', False),
                                       ('x_allocation_state', '=', 'unassigned')])
        assigned = self.browse()
        for vehicle in vehicles:
            driver = (vehicle['x_bas_driver_id'] if 'x_bas_driver_id' in vehicle._fields else False) \
                or vehicle.driver_id
            department = driver.department_id._ksw_fleet_holder() if driver.department_id else False
            if department:
                vehicle.write({'x_department_id': department.id})
                assigned |= vehicle
        _logger.info('Vehicle allocation: %s of %s unassigned vehicles placed from their driver.',
                     len(assigned), len(vehicles))
        return assigned


def _note(note):
    return Markup('<br/><i>%s</i>') % note if note else Markup('')
