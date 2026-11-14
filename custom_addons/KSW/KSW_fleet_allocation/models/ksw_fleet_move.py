from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.fields import Domain
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
    # A checkup decided as the first leg of a transfer: where the vehicle
    # goes when the checkup completes.
    reserved_department_id = fields.Many2one('hr.department', string='Then To', readonly=True)
    workshop_request_id = fields.Many2one('ksw.workshop.request', string='Workshop Request',
                                          ondelete='set null')
    workshop_state = fields.Selection(related='workshop_request_id.state', string='Workshop Status')

    x_can_receive = fields.Boolean(compute='_compute_can_receive')
    # "On its way to one of my departments": the Transfers to Receive list.
    # Home ▸ Waiting for you: a vehicle on its way to one of my departments.
    x_is_pending_my_action = fields.Boolean(compute='_compute_is_pending_my_action',
                                            search='_search_is_pending_my_action', string='Waiting for Me')
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

    @api.model
    def _ksw_receipt_by_department(self):
        """Does the receiving department confirm arrival here? Yes on its
        own; Vehicle Custody says no: the workshop handing the vehicle to the
        new driver (his HR-12) is the arrival, one confirmation per event."""
        return True

    @api.depends_context('uid')
    @api.depends('state', 'to_department_id')
    def _compute_is_pending_my_action(self):
        scope = set(self.env.user._ksw_fleet_department_ids())
        by_department = self._ksw_receipt_by_department()
        for move in self:
            move.x_is_pending_my_action = by_department and move.state == 'in_transfer' \
                and move.to_department_id.id in scope

    def _search_is_pending_my_action(self, operator, value):
        if operator in ('in', 'not in'):
            wanted = (operator == 'in') == any(value)
        elif operator in ('=', '!='):
            wanted = (operator == '=') == bool(value)
        else:
            return NotImplemented
        if not self._ksw_receipt_by_department():
            return [('id', '=', False)] if wanted else []
        domain = Domain([('state', '=', 'in_transfer'),
                         ('to_department_id', 'in', self.env.user._ksw_fleet_department_ids())])
        return domain if wanted else ~domain

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
            move.x_can_receive = pending and move.to_department_id.id in scope \
                and self._ksw_receipt_by_department()
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
            # (A vehicle in use first goes to the workshop: a holding move
            # with the transfer reserved.)
            def _taken(m):
                return (m.kind == 'transfer' or m.reserved_department_id) and not m.release_line_id \
                    and m.from_department_id and m.addition_line_id
            for source in moves.filtered(_taken).from_department_id:
                taken = moves.filtered(lambda m: _taken(m) and m.from_department_id == source)
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

    def _ksw_fleet_check_receivable(self):
        """Hook: what must be true before arrival can be confirmed (the
        custody module requires the new driver's handover form)."""
        return True

    def _ksw_fleet_on_cancel(self):
        """Hook: a cancelled transfer takes its paperwork with it."""
        return True

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
        received_condition, received_note}}."""
        self._check_can_receive()
        self._ksw_fleet_check_receivable()
        return self._ksw_fleet_complete(details)

    def _ksw_fleet_complete(self, details):
        """The transfer is done: the vehicle belongs to the receiving
        department. Called after the receiving side confirmed, or directly
        for a vehicle that moves together with its driver."""
        now = fields.Datetime.now()
        conditions = dict(self._fields['received_condition']._description_selection(self.env))
        gm_partners = self.env.ref('KSW_fleet_allocation.group_fleet_allocation_gm').sudo().all_user_ids.partner_id
        for move in self.sudo():
            vals = details.get(move.id, {})
            move.write(dict(vals, state='done', received_by_id=self.env.user.id, received_date=now))
            move.vehicle_id.with_context(ksw_fleet_allocation=True).write({
                'x_department_id': move.to_department_id.id, 'x_allocation_state': 'assigned'})
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
            move._ksw_fleet_on_cancel()
            back = move.from_state if move.from_state in ('assigned', 'holding', 'unassigned') \
                else ('assigned' if move.from_department_id else 'holding')
            move.vehicle_id.with_context(ksw_fleet_allocation=True).write({'x_allocation_state': back})
            # The release goes back to the GM's queue undecided, unless the
            # vehicle already left its department for the workshop: then it
            # simply stays in the pool.
            if move.release_line_id.decision == 'transfer':
                move.release_line_id.write(
                    {'decision': 'holding', 'target_department_id': False} if back == 'holding' else {
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
