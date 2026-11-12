from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class KswFleetMove(models.Model):
    """One change of a vehicle's department: the vehicle's history, and for a
    transfer the handover the receiving department confirms."""
    _name = 'ksw.fleet.move'
    _description = 'Vehicle Movement'
    _order = 'create_date desc, id desc'

    vehicle_id = fields.Many2one('ksw.fleet.vehicle', string='Vehicle', required=True,
                                 index=True, ondelete='cascade')
    vehicle_type = fields.Selection(related='vehicle_id.vehicle_type', store=True)
    kind = fields.Selection([
        ('initial', 'Starting Assignment'),
        ('manual', 'Set by Administrator'),
        ('transfer', 'Transfer'),
        ('holding', 'Sent to Workshop (Holding)'),
    ], required=True, string='Type')
    from_department_id = fields.Many2one('hr.department', string='From', index=True)
    # Empty on a holding move: the vehicle belongs to no department meanwhile.
    to_department_id = fields.Many2one('hr.department', string='To', index=True)
    # Where the vehicle was before this move, so cancelling a transfer puts it
    # back exactly (a vehicle taken from the Holding pool returns there).
    from_state = fields.Selection([
        ('unassigned', 'Unassigned'), ('assigned', 'Assigned'),
        ('in_transfer', 'In Transfer'), ('holding', 'Holding (Workshop)'),
    ])
    state = fields.Selection([
        ('in_transfer', 'In Transfer'),
        ('done', 'Done'),
        ('cancelled', 'Cancelled'),
    ], required=True, default='in_transfer', index=True)
    decided_by_id = fields.Many2one('res.users', string='Decided By')
    date = fields.Datetime(string='Decided On', default=fields.Datetime.now, required=True)
    received_by_id = fields.Many2one('res.users', string='Received By')
    received_date = fields.Datetime(string='Received On')
    note = fields.Text()
    # Recorded by the receiving department at handover, so a dispute about
    # the state it arrived in has evidence.
    received_odometer = fields.Integer(string='Odometer at Arrival', aggregator=None)
    received_condition = fields.Selection([
        ('good', 'Good'),
        ('minor', 'Minor Issues'),
        ('damaged', 'Damaged'),
    ], string='Condition at Arrival')
    received_note = fields.Text(string='Arrival Note')

    release_line_id = fields.Many2one('ksw.fleet.release.line', string='Release Line',
                                      index=True, ondelete='set null')
    addition_line_id = fields.Many2one('ksw.fleet.addition.line', string='Addition Line',
                                       index=True, ondelete='set null')
    request_id = fields.Many2one('ksw.fleet.request', compute='_compute_request_id', store=True,
                                 string='Request')
    # How an addition line was met: a vehicle the company already had, or one
    # obtained outside (purchased / rented) and allocated on arrival.
    source = fields.Selection([('internal', 'Internal'), ('external', 'External')],
                              default='internal')
    workshop_request_id = fields.Many2one('ksw.workshop.request', string='Workshop Request',
                                          ondelete='set null')
    workshop_state = fields.Selection(related='workshop_request_id.state', string='Workshop Status')

    handover_ids = fields.One2many('ksw.vehicle.handover', 'move_id', string='Handover Forms')
    x_can_receive = fields.Boolean(compute='_compute_can_receive')
    # "On its way to one of my departments": the Transfers to Receive list.
    x_is_incoming = fields.Boolean(string='Incoming to Me', compute='_compute_is_incoming',
                                   search='_search_is_incoming')
    x_can_cancel = fields.Boolean(compute='_compute_can_receive')

    @api.depends('addition_line_id.request_id', 'release_line_id.request_id')
    def _compute_request_id(self):
        for move in self:
            move.request_id = move.addition_line_id.request_id or move.release_line_id.request_id

    def _compute_display_name(self):
        for move in self:
            move.display_name = '%s: %s → %s' % (
                move.vehicle_id.name, move.from_department_id.name or _('Pool'),
                move.to_department_id.name or _('Holding'))

    @api.depends_context('uid')
    @api.depends('to_department_id')
    def _compute_is_incoming(self):
        scope = set(self.env.user._ksw_fleet_department_ids())
        for move in self:
            move.x_is_incoming = move.to_department_id.id in scope

    def _search_is_incoming(self, operator, value):
        # Odoo 19 hands a boolean condition over as in/not in (gotcha #24).
        if operator in ('in', 'not in'):
            wanted = (operator == 'in') == any(value)
        elif operator in ('=', '!='):
            wanted = (operator == '=') == bool(value)
        else:
            return NotImplemented
        ids = self.env.user._ksw_fleet_department_ids()
        if wanted:
            return [('to_department_id', 'in', ids)]
        return ['|', ('to_department_id', '=', False), ('to_department_id', 'not in', ids)]

    @api.depends_context('uid')
    @api.depends('state', 'to_department_id')
    def _compute_can_receive(self):
        is_gm = self.env['ksw.fleet.request']._ksw_fleet_is_gm()
        scope = set(self.env.user._ksw_fleet_department_ids())
        for move in self:
            pending = move.state == 'in_transfer'
            # Only the receiving department confirms the vehicle reached it:
            # nobody else, the GM included, can see it arrive. A GM who is
            # also named on that department receives as its user.
            move.x_can_receive = pending and move.to_department_id.id in scope
            move.x_can_cancel = pending and is_gm

    def _ksw_fleet_post_summary(self, note=False):
        """One chatter message per request for a batch of GM decisions,
        instead of one per vehicle. The receiving department is notified on
        request that sends it a vehicle by a Confirm Arrival activity (it may
        read those, see the read-only rules in security.xml)."""
        note_part = Markup('<br/><i>%s</i>') % note if note else Markup('')
        requests = self.release_line_id.request_id | self.addition_line_id.request_id
        for request in requests:
            moves = self.filtered(lambda m: request in (m.release_line_id.request_id | m.addition_line_id.request_id))
            parts, partners = [], self.env['res.partner']
            for target in moves.filtered(lambda m: m.kind == 'transfer').to_department_id:
                group = moves.filtered(lambda m: m.kind == 'transfer' and m.to_department_id == target)
                parts.append(Markup(_('<b>%(v)s</b> → <b>%(d)s</b>, to confirm on arrival under '
                                    'Vehicle Allocation ▸ Transfers to Receive.'))
                             % {'v': ', '.join(group.vehicle_id.mapped('name')), 'd': target.name})
            # Taken from a department that never released it: that department
            # is told, on the request it can now read (security.xml).
            for source in moves.filtered(
                    lambda m: m.kind == 'transfer' and not m.release_line_id and m.from_department_id
            ).from_department_id:
                taken = moves.filtered(lambda m: m.kind == 'transfer' and not m.release_line_id
                                       and m.from_department_id == source)
                parts.append(Markup(_('Taken from <b>%(d)s</b> by the GM\'s decision: <b>%(v)s</b>.'))
                             % {'d': source.name, 'v': ', '.join(taken.vehicle_id.mapped('name'))})
                partners |= source._ksw_fleet_users().partner_id
            held = moves.filtered(lambda m: m.kind == 'holding')
            if held:
                parts.append(Markup(_('<b>%(v)s</b> sent to the workshop for a checkup (%(w)s), now in '
                                    'the Holding pool.'))
                             % {'v': ', '.join(held.vehicle_id.mapped('name')),
                                'w': ', '.join(held.workshop_request_id.mapped('name'))})
            if parts:
                request._ksw_fleet_post(Markup('<br/>').join(parts) + note_part, partners=partners)

    def action_open_handover_form(self):
        self.ensure_one()
        form = self.handover_ids.filtered(lambda f: f.state != 'cancelled')[:1]
        if not form:
            raise UserError(_('This transfer has no handover form.'))
        return {'type': 'ir.actions.act_window', 'name': form.name, 'res_model': 'ksw.vehicle.handover',
                'res_id': form.id, 'view_mode': 'form'}

    def _check_can_receive(self):
        for move in self:
            if not move.x_can_receive:
                raise UserError(_(
                    'Only the receiving department confirms that %s arrived.',
                    move.vehicle_id.name))

    def action_receive(self):
        """Opens the handover form: odometer and condition per vehicle."""
        self._check_can_receive()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Confirm Arrival'),
            'res_model': 'ksw.fleet.receive.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_line_ids': [(0, 0, {'move_id': move.id}) for move in self]},
        }

    def _ksw_fleet_receive(self, details):
        """Complete these transfers. `details`: {move id: {received_odometer,
        received_condition, received_note}}. The receiving department names
        the new driver on HR-12 first; he becomes the vehicle's driver."""
        self._check_can_receive()
        unnamed = self.sudo().filtered(lambda m: m.handover_ids.filtered(
            lambda f: f.state != 'cancelled' and not f.new_employee_id))
        if unnamed:
            raise UserError(_(
                'Name the new driver on the handover form (HR-12) first: %s.',
                ', '.join(unnamed.vehicle_id.mapped('name'))))
        now = fields.Datetime.now()
        conditions = dict(self._fields['received_condition']._description_selection(self.env))
        gm_partners = self.env.ref('KSW_fleet_allocation.group_fleet_allocation_gm').sudo().all_user_ids.partner_id
        for move in self.sudo():
            vals = details.get(move.id, {})
            move.write(dict(vals, state='done', received_by_id=self.env.user.id, received_date=now))
            move.vehicle_id.with_context(ksw_fleet_allocation=True).write({
                'x_department_id': move.to_department_id.id, 'x_allocation_state': 'assigned'})
            new_driver = move.handover_ids.filtered(lambda f: f.state != 'cancelled')[:1].new_employee_id
            if new_driver:
                move.vehicle_id.driver_id = new_driver
            if move.request_id:
                condition = move.received_condition or 'good'
                body = Markup(_('<b>%(v)s</b> received by <b>%(d)s</b> (%(u)s). Condition: <b>%(c)s</b>%(o)s.')) % {
                    'v': move.vehicle_id.name, 'd': move.to_department_id.name, 'u': self.env.user.name,
                    'c': conditions[condition],
                    'o': Markup(_(', odometer %s')) % move.received_odometer if move.received_odometer else ''}
                if move.received_note:
                    body += Markup('<br/><i>%s</i>') % move.received_note
                # A vehicle that did not arrive in good order is the GM's to know.
                move.request_id._ksw_fleet_post(body, partners=gm_partners if condition != 'good' else None)
            (move.addition_line_id.request_id | move.release_line_id.request_id)._ksw_fleet_sync_state()
        return True

    def action_cancel(self):
        if not self.env['ksw.fleet.request']._ksw_fleet_is_gm():
            raise UserError(_('Only the GM cancels a transfer.'))
        for move in self:
            if move.state != 'in_transfer':
                raise UserError(_('Only a transfer still on its way can be cancelled.'))
        for move in self.sudo():
            move.state = 'cancelled'
            move.handover_ids._ksw_cancel(_('The transfer was cancelled by the GM.'))
            back = move.from_state if move.from_state in ('assigned', 'holding', 'unassigned') \
                else ('assigned' if move.from_department_id else 'holding')
            move.vehicle_id.with_context(ksw_fleet_allocation=True).write({'x_allocation_state': back})
            # The release goes back to the GM's queue undecided.
            if move.release_line_id.decision == 'transfer':
                move.release_line_id.write({
                    'decision': 'pending', 'target_department_id': False,
                    'decided_by_id': False, 'decided_date': False})
            requests = move.addition_line_id.request_id | move.release_line_id.request_id
            for request in requests:
                request._ksw_fleet_post(Markup(_('Transfer of <b>%(v)s</b> to %(d)s cancelled by the GM.'))
                                        % {'v': move.vehicle_id.name, 'd': move.to_department_id.name})
                if request.state == 'done':
                    request.state = 'submitted'
            requests._ksw_fleet_sync_state()
        return True
