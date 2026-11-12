from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class KswFleetAllocateWizard(models.TransientModel):
    _name = 'ksw.fleet.allocate.wizard'
    _description = 'Decide an Additional Vehicles Request'

    addition_line_id = fields.Many2one('ksw.fleet.addition.line', required=True, readonly=True)
    department_id = fields.Many2one(related='addition_line_id.department_id', string='For')
    vehicle_type = fields.Selection(related='addition_line_id.vehicle_type')
    quantity = fields.Integer(related='addition_line_id.quantity', string='Asked For')
    open_quantity = fields.Integer(related='addition_line_id.open_quantity')
    external_quantity = fields.Integer(related='addition_line_id.external_quantity', string='Decided External')
    external_received_quantity = fields.Integer(related='addition_line_id.external_received_quantity')
    external_waiting = fields.Integer(compute='_compute_external_waiting')

    mode = fields.Selection([
        ('internal', 'Give vehicles we already have'),
        ('external', 'Obtain outside (purchase / rent)'),
        ('external_arrival', 'Allocate an obtained vehicle that arrived'),
        ('refuse', 'Refuse'),
    ], required=True, default='internal')
    include_assigned = fields.Boolean(
        string='Also take from departments that did not release',
        help='Show vehicles still in use elsewhere, to take one the GM decides is better used here.')
    available_vehicle_ids = fields.Many2many('ksw.fleet.vehicle', compute='_compute_available_vehicle_ids')
    vehicle_ids = fields.Many2many('ksw.fleet.vehicle', string='Vehicles',
                                   domain="[('id', 'in', available_vehicle_ids)]")
    decide_quantity = fields.Integer(string='How Many', default=1)
    external_method = fields.Selection([('purchase', 'Purchase'), ('rent', 'Rent'), ('other', 'Other')],
                                       default='purchase')
    note = fields.Text()

    @api.depends('external_quantity', 'external_received_quantity')
    def _compute_external_waiting(self):
        for wizard in self:
            wizard.external_waiting = wizard.external_quantity - wizard.external_received_quantity

    @api.depends('addition_line_id', 'mode', 'include_assigned')
    def _compute_available_vehicle_ids(self):
        Vehicle = self.env['ksw.fleet.vehicle']
        for wizard in self:
            line = wizard.addition_line_id
            base = [('x_is_own_fleet', '=', True), ('vehicle_type', '=', line.vehicle_type),
                    ('state', '=', 'confirmed'), ('x_checkup_open', '=', False),
                    ('x_reserved_department_id', '=', False)]
            if wizard.mode == 'external_arrival':
                # A newly obtained vehicle is registered without a department.
                pool = [('x_allocation_state', '=', 'unassigned')]
            elif wizard.include_assigned:
                pool = ['|', ('x_allocation_state', 'in', ('holding', 'unassigned')),
                        '&', ('x_allocation_state', '=', 'assigned'),
                        ('x_department_id', '!=', line.department_id.id)]
            else:
                pool = ['|', ('x_allocation_state', 'in', ('holding', 'unassigned')),
                        '&', ('x_allocation_state', '=', 'assigned'), ('x_is_released', '=', True)]
            wizard.available_vehicle_ids = Vehicle.search(base + pool) if line else Vehicle

    def action_confirm(self):
        self.ensure_one()
        if not self.env['ksw.fleet.request']._ksw_fleet_is_gm():
            raise UserError(_('Only the GM decides an addition.'))
        line = self.addition_line_id.sudo()
        request = line.request_id
        if request.state != 'submitted':
            raise UserError(_('%s is not waiting for a decision.', request.name))
        note_part = Markup('<br/><i>%s</i>') % self.note if self.note else Markup('')
        moves = self.env['ksw.fleet.move']
        if self.mode in ('internal', 'external_arrival'):
            if not self.vehicle_ids:
                raise UserError(_('Choose the vehicles to give.'))
            limit = line.open_quantity if self.mode == 'internal' else self.external_waiting
            if len(self.vehicle_ids) > limit:
                raise UserError(_('Only %s more can be given on this line.', limit))
            outside = self.vehicle_ids - self.available_vehicle_ids
            if outside:
                raise UserError(_('Not available: %s.', ', '.join(outside.mapped('name'))))
            # Taking a vehicle its department never offered: say why, and that
            # department is told (_ksw_fleet_post_summary).
            taken = self.vehicle_ids.sudo().filtered(
                lambda v: v.x_allocation_state == 'assigned' and not v.x_is_released)
            if taken and not self.note:
                raise UserError(_(
                    '%s were not released by their department. Write the reason in the note: '
                    'that department is told.', ', '.join(taken.mapped('name'))))
            source = 'internal' if self.mode == 'internal' else 'external'
            for vehicle in self.vehicle_ids.sudo().with_context(ksw_fleet_batch=True):
                moves |= vehicle._ksw_fleet_start_transfer(line.department_id, self.note,
                                                           addition_line=line, source=source)
            moves._ksw_fleet_post_summary(self.note)
        elif self.mode == 'external':
            if not 0 < self.decide_quantity <= line.open_quantity:
                raise UserError(_('Between 1 and %s.', line.open_quantity))
            line.write({
                'external_quantity': line.external_quantity + self.decide_quantity,
                'external_method': self.external_method,
                'external_note': '\n'.join(filter(None, [line.external_note, self.note])),
            })
            request._ksw_fleet_post(Markup(
                _('%(q)s × %(t)s to be obtained outside (%(m)s). Each one is allocated to '
                '%(d)s when it arrives.%(n)s')) % {
                'q': self.decide_quantity, 't': dict(line._fields['vehicle_type']._description_selection(self.env))[line.vehicle_type],
                'm': dict(self._fields['external_method']._description_selection(self.env))[self.external_method],
                'd': line.department_id.name, 'n': note_part})
        else:
            if not 0 < self.decide_quantity <= line.open_quantity:
                raise UserError(_('Between 1 and %s.', line.open_quantity))
            if not self.note:
                raise UserError(_('Give the reason, so the department knows why.'))
            line.write({
                'refused_quantity': line.refused_quantity + self.decide_quantity,
                'refuse_reason': '\n'.join(filter(None, [line.refuse_reason, self.note])),
            })
            request._ksw_fleet_post(Markup(_('%(q)s × %(t)s refused.%(n)s')) % {
                'q': self.decide_quantity,
                't': dict(line._fields['vehicle_type']._description_selection(self.env))[line.vehicle_type], 'n': note_part})
        # A vehicle taken from a release settles that request too.
        (request | moves.release_line_id.request_id)._ksw_fleet_sync_state()
        return {'type': 'ir.actions.act_window_close'}
