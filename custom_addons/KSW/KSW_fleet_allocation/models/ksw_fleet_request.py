from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.fields import Domain
from odoo.exceptions import UserError, ValidationError

VEHICLE_TYPES = [
    ('trailer', 'Trailer (تريلا)'),
    ('isuzu', 'Isuzu'),
    ('tank', 'Tedder (تيدر)'),
    ('other', 'Other'),
]

# What a requester may change while the request is still his draft. After he
# submits, the request belongs to the GM's decisions.
_DRAFT_FIELDS = {'request_type', 'department_id', 'reason', 'release_line_ids', 'addition_line_ids'}


class KswFleetRequest(models.Model):
    _name = 'ksw.fleet.request'
    _description = 'Vehicle Allocation Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(default='New', copy=False, readonly=True)
    request_type = fields.Selection([
        ('release', 'Release Vehicles'),
        ('addition', 'Additional Vehicles'),
    ], string='Request', required=True, default='release', tracking=True)
    department_id = fields.Many2one(
        'hr.department', string='Department', required=True, index=True, tracking=True,
        default=lambda self: self._default_department_id(),
        domain="[('id', 'in', allowed_department_ids)]")
    allowed_department_ids = fields.Many2many('hr.department', compute='_compute_allowed_department_ids')
    user_id = fields.Many2one('res.users', string='Requested By', required=True, readonly=True,
                              default=lambda self: self.env.user)
    reason = fields.Text(required=True)
    submitted_date = fields.Datetime(readonly=True, copy=False)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('submitted', 'Waiting for GM'),
        ('done', 'Closed'),
        ('cancelled', 'Cancelled'),
    ], default='draft', required=True, tracking=True, copy=False, index=True)

    release_line_ids = fields.One2many('ksw.fleet.release.line', 'request_id', string='Vehicles to Release')
    addition_line_ids = fields.One2many('ksw.fleet.addition.line', 'request_id', string='Vehicles Needed')
    move_ids = fields.One2many('ksw.fleet.move', 'request_id', string='Movements')

    open_line_count = fields.Integer(compute='_compute_line_counts', string='Undecided')
    line_summary = fields.Char(compute='_compute_line_counts', string='Summary')
    x_is_gm = fields.Boolean(compute='_compute_is_gm')
    # Home ▸ Waiting for you, and the "Waiting for Me" filter: what this user
    # has to act on now. Other modules add their own steps (_ksw_pending_domain).
    x_is_pending_my_action = fields.Boolean(compute='_compute_is_pending_my_action',
                                            search='_search_is_pending_my_action',
                                            string='Waiting for Me')

    @api.model
    def _ksw_pending_domain(self):
        """Requests waiting for the current user (a list of OR-ed domains).
        The GM decides submitted requests that still have something open."""
        if not self._ksw_fleet_is_gm():
            return []
        return [['&', ('state', '=', 'submitted'), '|',
                 ('release_line_ids.decision', '=', 'pending'),
                 ('addition_line_ids.open_quantity', '>', 0)]]

    @api.depends_context('uid')
    def _compute_is_pending_my_action(self):
        domains = self._ksw_pending_domain()
        mine = set(self.search(Domain.OR(domains)).ids) if domains else set()
        for request in self:
            request.x_is_pending_my_action = request.id in mine

    def _search_is_pending_my_action(self, operator, value):
        # Odoo 19 hands a boolean condition over as in/not in (gotcha #24).
        if operator in ('in', 'not in'):
            wanted = (operator == 'in') == any(value)
        elif operator in ('=', '!='):
            wanted = (operator == '=') == bool(value)
        else:
            return NotImplemented
        domains = self._ksw_pending_domain()
        if not domains:
            return [('id', '=', False)] if wanted else []
        domain = Domain.OR(domains)
        return domain if wanted else ~domain

    @api.model
    def _ksw_fleet_is_gm(self):
        return self.env.su or self.env.user.has_group('KSW_fleet_allocation.group_fleet_allocation_gm')

    @api.model
    def _default_department_id(self):
        ids = self.env.user._ksw_fleet_department_ids()
        holders = self.env['hr.department'].sudo().search([('id', 'in', ids), ('x_fleet_holder', '=', True)])
        return holders[:1].id

    @api.depends_context('uid')
    def _compute_allowed_department_ids(self):
        Department = self.env['hr.department']
        domain = [('x_fleet_holder', '=', True)]
        if not self._ksw_fleet_is_gm():
            domain.append(('id', 'in', self.env.user._ksw_fleet_department_ids()))
        allowed = Department.search(domain)
        for request in self:
            request.allowed_department_ids = allowed

    @api.depends_context('uid')
    def _compute_is_gm(self):
        is_gm = self._ksw_fleet_is_gm()
        for request in self:
            request.x_is_gm = is_gm

    @api.depends('request_type', 'release_line_ids.status', 'addition_line_ids.status',
                 'addition_line_ids.quantity')
    def _compute_line_counts(self):
        for request in self:
            if request.request_type == 'release':
                lines = request.release_line_ids
                request.open_line_count = len(lines.filtered(lambda l: l.status == 'pending'))
                request.line_summary = ', '.join(lines.vehicle_id.mapped('name'))
            else:
                lines = request.addition_line_ids
                request.open_line_count = len(lines.filtered(lambda l: l.open_quantity > 0))
                types = dict(self.env['ksw.fleet.addition.line']._fields['vehicle_type']._description_selection(self.env))
                needs = dict(self.env['ksw.fleet.addition.line']._fields['need']._description_selection(self.env))
                request.line_summary = ', '.join(
                    '%s × %s (%s)' % (l.quantity, types.get(l.vehicle_type), needs.get(l.need)) for l in lines)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('ksw.fleet.request') or 'New'
            if not self.env.su:
                vals.pop('state', None)
                vals.pop('user_id', None)
        requests = super().create(vals_list)
        requests._check_department_scope()
        return requests

    def write(self, vals):
        if not self.env.su:
            if _DRAFT_FIELDS & set(vals) and self.filtered(lambda r: r.state != 'draft'):
                raise UserError(_('A submitted request cannot be changed. The GM decides it as it is; '
                                  'cancel it and raise a new one if it is wrong.'))
            if {'state', 'submitted_date', 'user_id'} & set(vals):
                raise UserError(_('Use the buttons to move a request on.'))
        res = super().write(vals)
        if 'department_id' in vals:
            self._check_department_scope()
        return res

    def _check_department_scope(self):
        if self.env.su or self._ksw_fleet_is_gm():
            return
        scope = set(self.env.user._ksw_fleet_department_ids())
        for request in self:
            if request.department_id.id not in scope:
                raise UserError(_('You do not answer for the vehicles of %s.', request.department_id.name))

    def unlink(self):
        if not self.env.su and self.filtered(lambda r: r.state not in ('draft', 'cancelled')):
            raise UserError(_('Only a draft or cancelled request can be deleted.'))
        return super().unlink()

    # ------------------------------------------------------------------
    # Requester actions
    # ------------------------------------------------------------------
    def action_submit(self):
        self._check_department_scope()
        for request in self:
            if request.state != 'draft':
                raise UserError(_('%s was already submitted.', request.name))
            if request.request_type == 'release':
                if not request.release_line_ids:
                    raise UserError(_('Add the vehicles you want to release.'))
                if request.addition_line_ids:
                    raise UserError(_('A release request lists vehicles to release only.'))
                request.release_line_ids._check_vehicles()
            else:
                if not request.addition_line_ids:
                    raise UserError(_('Add the type and number of vehicles you need.'))
                if request.release_line_ids:
                    raise UserError(_('An additional vehicles request lists vehicles needed only.'))
        self.sudo().write({'state': 'submitted', 'submitted_date': fields.Datetime.now()})
        for request in self:
            request.sudo().message_post(body=Markup(
                _('<b>%(t)s</b> from <b>%(d)s</b> waits for the GM\'s decision: %(s)s<br/><i>%(r)s</i>'))
                % {'t': dict(request._fields['request_type']._description_selection(self.env))[request.request_type],
                   'd': request.department_id.name, 's': request.line_summary, 'r': request.reason},
                subtype_xmlid='mail.mt_note')
        self._ksw_fleet_sync_activities()
        return True

    def action_cancel(self):
        for request in self:
            if request.state not in ('draft', 'submitted'):
                raise UserError(_('%s is already closed.', request.name))
            if not self._ksw_fleet_is_gm():
                request._check_department_scope()
            decided = request.release_line_ids.filtered(lambda l: l.decision != 'pending') \
                or request.addition_line_ids.filtered(
                    lambda l: l.move_ids.filtered(lambda m: m.state != 'cancelled')
                    or l.external_quantity or l.refused_quantity)
            if decided:
                raise UserError(_(
                    'The GM has already decided part of %s. Ask the GM to close what is left '
                    'instead of cancelling it.', request.name))
        self.sudo().write({'state': 'cancelled'})
        for request in self:
            request._ksw_fleet_post(Markup(_('Cancelled by %s.')) % self.env.user.name)
        self._ksw_fleet_sync_activities()
        return True

    def action_decide_release_lines(self):
        """Header button: every undecided vehicle of this request in one
        dialog; the GM removes the ones he decides otherwise."""
        self.ensure_one()
        return self.release_line_ids.action_open_decision()

    def action_reset_draft(self):
        for request in self:
            if request.state != 'cancelled':
                raise UserError(_('Only a cancelled request goes back to draft.'))
            request._check_department_scope()
        self.sudo().write({'state': 'draft', 'submitted_date': False})
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _ksw_fleet_post(self, body, partners=None):
        """Chatter note on the request; the requester is always told, plus
        any `partners` named. sudo(): the caller's authority was checked
        before, and he may not write the request (gotcha #11)."""
        for request in self.sudo():
            recipients = (partners or self.env['res.partner']) | request.user_id.partner_id
            recipients -= self.env.user.partner_id
            request.message_post(body=body, partner_ids=recipients.ids,
                                 subtype_xmlid='mail.mt_comment' if recipients else 'mail.mt_note')

    def _ksw_fleet_sync_state(self):
        """Closed once every line is settled. A summary of the lines, never
        set by a button. Also brings the to-dos in line with it."""
        for request in self.sudo():
            if request.state != 'submitted':
                continue
            lines = request.release_line_ids if request.request_type == 'release' else request.addition_line_ids
            if lines and all(lines.mapped('is_closed')):
                request.state = 'done'
                request._ksw_fleet_post(Markup(_('All lines are settled; the request is closed.')))
        self._ksw_fleet_sync_activities()

    def _ksw_fleet_sync_activities(self):
        """Desired-vs-actual, so any route (decide, receive, cancel a
        transfer, cancel the request) leaves exactly the to-dos still owed:
        a Decide for every GM while lines wait for him, and a Confirm Arrival
        for the receiving department's users while a vehicle is on its way.
        A move on both a release and an addition counts on the addition (the
        receiver's own request)."""
        decide = self.env.ref('KSW_fleet_allocation.mail_activity_fleet_decide')
        receive = self.env.ref('KSW_fleet_allocation.mail_activity_fleet_receive')
        gm_users = self.env.ref('KSW_fleet_allocation.group_fleet_allocation_gm').sudo() \
            .all_user_ids.filtered(lambda u: u.active and not u.share)
        Move = self.env['ksw.fleet.move'].sudo()
        for request in self.sudo().exists():
            wanted = {}
            if request.state == 'submitted' and request.open_line_count:
                for user in gm_users:
                    wanted[(decide.id, user.id)] = _('Decide: %(d)s, %(s)s',
                                                     d=request.department_id.name, s=request.line_summary)
            moving = Move.search([('request_id', '=', request.id), ('state', '=', 'in_transfer')]) \
                if Move._ksw_receipt_by_department() else Move
            for department in moving.to_department_id:
                names = ', '.join(moving.filtered(lambda m: m.to_department_id == department).vehicle_id.mapped('name'))
                for user in department._ksw_fleet_users():
                    wanted[(receive.id, user.id)] = _('Confirm arrival: %s', names)
            for activity in request.activity_ids.filtered(lambda a: a.activity_type_id in (decide | receive)):
                key = (activity.activity_type_id.id, activity.user_id.id)
                if key in wanted:
                    activity.summary = wanted.pop(key)
                else:
                    activity.unlink()
            for (type_id, user_id), summary in wanted.items():
                request.activity_schedule(activity_type_id=type_id, user_id=user_id, summary=summary)


class KswFleetReleaseLine(models.Model):
    _name = 'ksw.fleet.release.line'
    _description = 'Vehicle to Release'
    _order = 'request_id desc, id'

    request_id = fields.Many2one('ksw.fleet.request', required=True, index=True, ondelete='cascade')
    request_state = fields.Selection(related='request_id.state', string='Request Status')
    department_id = fields.Many2one(related='request_id.department_id', store=True, string='Department')
    vehicle_id = fields.Many2one(
        'ksw.fleet.vehicle', string='Vehicle', required=True, index=True,
        domain="[('x_department_id', 'child_of', department_id), ('x_allocation_state', '=', 'assigned')]")
    vehicle_type = fields.Selection(related='vehicle_id.vehicle_type', store=True)
    plate_number = fields.Char(related='vehicle_id.plate_number')
    driver_name = fields.Char(related='vehicle_id.driver_name', string='Driver')
    note = fields.Char(string='Why')
    # Part of the intent: the vehicle goes with the employee who answers for
    # it. It can only fill a need that accepts a driver; executed by the
    # Vehicles-with-Drivers bridge (no workshop, the driver moves too).
    x_with_driver = fields.Boolean(string='With Driver',
                                   help='The vehicle moves together with the employee who answers for it: '
                                        'no workshop, no handover form, and he is transferred too.')

    decision = fields.Selection([
        ('pending', 'Waiting for GM'),
        ('transfer', 'Transfer'),
        ('holding', 'To Workshop / Holding'),
        ('keep', 'Kept by Department'),
    ], default='pending', required=True, index=True, copy=False)
    target_department_id = fields.Many2one('hr.department', string='Transfer To', copy=False)
    decision_note = fields.Text(string='GM Note', copy=False)
    decided_by_id = fields.Many2one('res.users', string='Decided By', copy=False)
    decided_date = fields.Datetime(string='Decided On', copy=False)
    move_ids = fields.One2many('ksw.fleet.move', 'release_line_id', string='Movements')

    status = fields.Selection([
        ('pending', 'Waiting for GM'),
        ('checkup', 'At Workshop Checkup'),
        ('in_transfer', 'In Transfer'),
        ('transferred', 'Transferred'),
        ('holding', 'In Holding'),
        ('kept', 'Kept'),
        ('cancelled', 'Cancelled'),
    ], compute='_compute_status', store=True)
    is_closed = fields.Boolean(compute='_compute_status', store=True)

    @api.depends('decision', 'request_id.state', 'move_ids.state')
    def _compute_status(self):
        for line in self:
            if line.request_id.state == 'cancelled':
                status = 'cancelled'
            elif line.decision == 'transfer':
                live = line.move_ids.filtered(lambda m: m.kind == 'transfer' and m.state != 'cancelled')[:1]
                status = 'checkup' if not live else (
                    'in_transfer' if live.state == 'in_transfer' else 'transferred')
            else:
                status = {'pending': 'pending', 'holding': 'holding', 'keep': 'kept'}[line.decision]
            line.status = status
            line.is_closed = status not in ('pending', 'checkup', 'in_transfer')

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            for vals in vals_list:
                for key in ('decision', 'target_department_id', 'decision_note', 'decided_by_id', 'decided_date'):
                    vals.pop(key, None)
        lines = super().create(vals_list)
        lines._check_draft()
        return lines

    def write(self, vals):
        if not self.env.su:
            if {'decision', 'target_department_id', 'decision_note', 'decided_by_id', 'decided_date'} & set(vals):
                raise UserError(_('The GM decides a release with the Decide button.'))
            self._check_draft()
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            self._check_draft()
        return super().unlink()

    def _check_draft(self):
        if self.env.su:
            return
        if self.filtered(lambda l: l.request_id.state != 'draft'):
            raise UserError(_('A submitted request cannot be changed.'))

    def _check_vehicles(self):
        for line in self:
            vehicle = line.vehicle_id.sudo()
            if not vehicle.x_department_id or vehicle.x_department_id._ksw_fleet_holder() \
                    != line.request_id.department_id._ksw_fleet_holder():
                raise UserError(_('%(v)s is not a vehicle of %(d)s.',
                                  v=vehicle.name, d=line.request_id.department_id.name))
            if vehicle.x_allocation_state != 'assigned':
                raise UserError(_('%s is already being moved.', vehicle.name))
            if vehicle.x_is_released:
                raise UserError(_('%(v)s is already on another release request (%(r)s).',
                                  v=vehicle.name, r=vehicle.x_release_request_id.name))
        if len(self.vehicle_id) != len(self):
            raise UserError(_('A vehicle is listed twice.'))

    def _ksw_check_fills(self, addition_line):
        """A released vehicle goes only to a department that asked for one:
        same type, a need that accepts it, and room left on that need."""
        addition_line = addition_line.sudo()
        if not addition_line or addition_line.request_id.state != 'submitted':
            raise UserError(_('Choose the open request the vehicles go to.'))
        for line in self:
            if line.vehicle_type != addition_line.vehicle_type:
                raise UserError(_('%(v)s is not a %(t)s, which is what %(r)s asks for.',
                                  v=line.vehicle_id.name, r=addition_line.request_id.name,
                                  t=dict(addition_line._fields['vehicle_type']._description_selection(self.env))[addition_line.vehicle_type]))
            if not addition_line._ksw_accepts(line.x_with_driver):
                raise UserError(_('%(r)s needs %(n)s; %(v)s is released %(w)s.',
                                  r=addition_line.request_id.name,
                                  n=dict(addition_line._fields['need']._description_selection(self.env))[addition_line.need],
                                  v=line.vehicle_id.name,
                                  w=_('with its driver') if line.x_with_driver else _('without a driver')))
            if line.department_id == addition_line.department_id:
                raise UserError(_('%s already belongs to that department.', line.vehicle_id.name))
        if len(self) > addition_line.open_quantity:
            raise UserError(_('%(r)s still needs %(q)s; you chose %(n)s.', r=addition_line.request_id.name,
                              q=addition_line.open_quantity, n=len(self)))

    def _ksw_execute_transfer(self, addition_line, note=False):
        """The GM gave these released vehicles to a request: through the
        workshop (checkup, then the new driver's handover). A vehicle released
        with its driver is moved by the Vehicles-with-Drivers bridge."""
        moves = self.env['ksw.fleet.move']
        if self.filtered('x_with_driver'):
            raise UserError(_('Vehicles released with their drivers need the "Vehicles with Their '
                              'Drivers" module.'))
        for line in self:
            moves |= line.vehicle_id._ksw_fleet_start_transfer(
                addition_line.department_id, note, addition_line=addition_line)
        return moves

    def action_open_decision(self):
        """The decision dialog for these lines: one row's Decide button, or
        every vehicle ticked in Released Vehicles. Already-decided ones are
        left out rather than refused, so a mixed selection still works."""
        if not self.env['ksw.fleet.request']._ksw_fleet_is_gm():
            raise UserError(_('Only the GM decides a release.'))
        pending = self.filtered(lambda l: l.status == 'pending' and l.request_id.state == 'submitted')
        if not pending:
            raise UserError(_('None of these vehicles is waiting for a decision.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Decide: %s', pending.vehicle_id.name) if len(pending) == 1
            else _('Decide %s Vehicles', len(pending)),
            'res_model': 'ksw.fleet.release.decision.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_release_line_ids': [(6, 0, pending.ids)]},
        }


class KswFleetAdditionLine(models.Model):
    _name = 'ksw.fleet.addition.line'
    _description = 'Vehicles Needed'
    _order = 'request_id desc, id'

    request_id = fields.Many2one('ksw.fleet.request', required=True, index=True, ondelete='cascade')
    request_state = fields.Selection(related='request_id.state', string='Request Status')
    department_id = fields.Many2one(related='request_id.department_id', store=True, string='Department')
    vehicle_type = fields.Selection(VEHICLE_TYPES, string='Vehicle Type', required=True)
    quantity = fields.Integer(default=1, required=True)
    # What fills it: a vehicle alone (the department has the driver), a
    # vehicle with its driver, or either.
    need = fields.Selection([
        ('vehicle', 'Vehicle Only'),
        ('with_driver', 'Vehicle with Driver'),
        ('either', 'Either'),
    ], string='Needs', required=True, default='vehicle')
    note = fields.Char(string='What For')

    move_ids = fields.One2many('ksw.fleet.move', 'addition_line_id', string='Movements')
    internal_quantity = fields.Integer(compute='_compute_quantities', store=True, string='Internal')
    # The GM's decision to obtain vehicles outside; each one is allocated to
    # this line from the pool when it arrives (external_received_quantity).
    external_quantity = fields.Integer(string='External', copy=False)
    external_method = fields.Selection([
        ('purchase', 'Purchase'), ('rent', 'Rent'), ('other', 'Other'),
    ], string='External Method', copy=False)
    external_note = fields.Text(copy=False)
    external_received_quantity = fields.Integer(compute='_compute_quantities', store=True,
                                                string='External Arrived')
    refused_quantity = fields.Integer(string='Refused', copy=False)
    refuse_reason = fields.Text(copy=False)
    open_quantity = fields.Integer(compute='_compute_quantities', store=True, string='Undecided')

    status = fields.Selection([
        ('pending', 'Waiting for GM'),
        ('waiting_external', 'Waiting for External'),
        ('in_transfer', 'In Transfer'),
        ('done', 'Done'),
        ('refused', 'Refused'),
        ('cancelled', 'Cancelled'),
    ], compute='_compute_quantities', store=True)
    is_closed = fields.Boolean(compute='_compute_quantities', store=True)

    _quantity_positive = models.Constraint('CHECK(quantity > 0)', 'The quantity must be at least 1.')

    @api.depends('quantity', 'external_quantity', 'refused_quantity', 'request_id.state',
                 'move_ids.state', 'move_ids.source', 'move_ids.kind', 'move_ids.vehicle_id')
    def _compute_quantities(self):
        for line in self:
            live = line.move_ids.filtered(lambda m: m.state != 'cancelled')
            # Vehicles, not moves: a vehicle reserved for this line has its
            # checkup move first, then its transfer.
            line.internal_quantity = len(live.filtered(lambda m: m.source == 'internal').vehicle_id)
            line.external_received_quantity = len(live.filtered(lambda m: m.source == 'external').vehicle_id)
            arrived = live.filtered(lambda m: m.kind == 'transfer' and m.state == 'done').vehicle_id
            line.open_quantity = line.quantity - line.internal_quantity \
                - line.external_quantity - line.refused_quantity
            if line.request_id.state == 'cancelled':
                status = 'cancelled'
            elif line.open_quantity > 0:
                status = 'pending'
            elif line.external_received_quantity < line.external_quantity:
                status = 'waiting_external'
            elif live.vehicle_id - arrived:
                status = 'in_transfer'
            elif line.refused_quantity == line.quantity:
                status = 'refused'
            else:
                status = 'done'
            line.status = status
            line.is_closed = status in ('done', 'refused', 'cancelled')

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            for vals in vals_list:
                for key in self._decision_fields():
                    vals.pop(key, None)
        lines = super().create(vals_list)
        lines._check_draft()
        return lines

    def write(self, vals):
        if not self.env.su:
            if self._decision_fields() & set(vals):
                raise UserError(_('The GM decides an addition with the Decide button.'))
            self._check_draft()
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            self._check_draft()
        return super().unlink()

    def _ksw_accepts(self, with_driver):
        self.ensure_one()
        return self.need == 'either' or self.need == ('with_driver' if with_driver else 'vehicle')

    @api.model
    def _decision_fields(self):
        return {'external_quantity', 'external_method', 'external_note', 'refused_quantity', 'refuse_reason'}

    def _check_draft(self):
        if self.env.su:
            return
        if self.filtered(lambda l: l.request_id.state != 'draft'):
            raise UserError(_('A submitted request cannot be changed.'))

    @api.constrains('quantity', 'external_quantity', 'refused_quantity')
    def _check_quantities(self):
        for line in self:
            if line.external_quantity < 0 or line.refused_quantity < 0:
                raise ValidationError(_('Quantities cannot be negative.'))
            if line.open_quantity < 0:
                raise ValidationError(_('More vehicles were decided than the %s asked for.', line.quantity))

    def action_open_decision(self):
        self.ensure_one()
        if not self.env['ksw.fleet.request']._ksw_fleet_is_gm():
            raise UserError(_('Only the GM decides an addition.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Decide: %(q)s × %(t)s for %(d)s', q=self.quantity,
                      t=dict(self.env['ksw.fleet.addition.line']._fields['vehicle_type']._description_selection(self.env)).get(self.vehicle_type), d=self.department_id.name),
            'res_model': 'ksw.fleet.allocate.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_addition_line_id': self.id},
        }
