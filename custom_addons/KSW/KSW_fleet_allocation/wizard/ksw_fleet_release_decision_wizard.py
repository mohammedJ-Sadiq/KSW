from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class KswFleetReleaseDecisionWizard(models.TransientModel):
    """One decision for any number of released vehicles. The GM ticks them
    (on a request, or across requests in Released Vehicles), drops the ones
    he wants to decide otherwise, and confirms; the rest stay waiting."""
    _name = 'ksw.fleet.release.decision.wizard'
    _description = 'Decide Released Vehicles'

    release_line_ids = fields.Many2many(
        'ksw.fleet.release.line', string='Vehicles', required=True,
        domain="[('status', '=', 'pending'), ('request_state', '=', 'submitted')]")
    source_department_ids = fields.Many2many('hr.department', compute='_compute_sources')
    vehicle_types = fields.Char(compute='_compute_sources')
    decision = fields.Selection([
        ('transfer', 'Transfer to another department (after a workshop checkup)'),
        ('holding', 'Send to workshop and hold'),
        ('keep', 'Keep them where they are'),
    ], required=True, default='transfer')
    target_department_id = fields.Many2one(
        'hr.department', string='Transfer To',
        domain="[('x_fleet_holder', '=', True), ('id', 'not in', source_department_ids)]")
    match_requests = fields.Boolean(
        string="Count against its open requests", default=True,
        help="Each vehicle settles the receiving department's oldest open request for its "
             "type, until those are met. The rest are transferred without a request.")
    demand_line_ids = fields.Many2many('ksw.fleet.addition.line', compute='_compute_demand_line_ids',
                                       string='Departments Asking for these Types')
    workshop_description = fields.Text(string='For the Workshop',
                                       default=lambda self: _('Checkup before reallocation'))
    note = fields.Text(string='Note')

    @api.depends('release_line_ids')
    def _compute_sources(self):
        types = dict(self.env['ksw.fleet.vehicle']._fields['vehicle_type']._description_selection(self.env))
        for wizard in self:
            wizard.source_department_ids = wizard.release_line_ids.department_id
            wizard.vehicle_types = ', '.join(sorted({
                types.get(t, '') for t in wizard.release_line_ids.mapped('vehicle_type') if t}))

    @api.depends('release_line_ids')
    def _compute_demand_line_ids(self):
        for wizard in self:
            wizard.demand_line_ids = self.env['ksw.fleet.addition.line'].search([
                ('vehicle_type', 'in', wizard.release_line_ids.mapped('vehicle_type')),
                ('open_quantity', '>', 0), ('request_id.state', '=', 'submitted')])

    def action_confirm(self):
        self.ensure_one()
        if not self.env['ksw.fleet.request']._ksw_fleet_is_gm():
            raise UserError(_('Only the GM decides a release.'))
        lines = self.release_line_ids.sudo()
        if not lines:
            raise UserError(_('Tick at least one vehicle.'))
        decided = lines.filtered(lambda l: l.decision != 'pending' or l.request_id.state != 'submitted')
        if decided:
            raise UserError(_('Already decided: %s. Remove them from the list.',
                              ', '.join(decided.vehicle_id.mapped('name'))))
        target = self.target_department_id
        if self.decision == 'keep' and not self.note:
            raise UserError(_('Write why they are kept: the department asked to give them up.'))
        if self.decision == 'transfer':
            if not target:
                raise UserError(_('Choose the department that receives them.'))
            own = lines.filtered(lambda l: l.department_id == target)
            if own:
                raise UserError(_('%(v)s already belong to %(d)s.',
                                  v=', '.join(own.vehicle_id.mapped('name')), d=target.name))
        Addition = self.env['ksw.fleet.addition.line'].sudo()
        moves = self.env['ksw.fleet.move']
        note_part = Markup('<br/><i>%s</i>') % self.note if self.note else Markup('')
        for line in lines.with_context(ksw_fleet_batch=True):
            vehicle = line.vehicle_id
            if self.decision == 'transfer':
                # search() flushes, so open_quantity already counts the
                # vehicles given earlier in this same loop.
                demand = Addition.search([
                    ('department_id', '=', target.id), ('vehicle_type', '=', vehicle.vehicle_type),
                    ('open_quantity', '>', 0), ('request_id.state', '=', 'submitted'),
                ], order='id', limit=1) if self.match_requests else Addition
                moves |= vehicle._ksw_fleet_start_transfer(target, self.note, addition_line=demand or None)
            elif self.decision == 'holding':
                moves |= vehicle._ksw_fleet_send_to_holding(self.workshop_description, self.note)
            else:
                line.write({'decision': 'keep', 'decision_note': self.note,
                            'decided_by_id': self.env.user.id, 'decided_date': fields.Datetime.now()})
        moves._ksw_fleet_post_summary(self.note)
        if self.decision == 'keep':
            for request in lines.request_id:
                names = ', '.join(lines.filtered(lambda l: l.request_id == request).vehicle_id.mapped('name'))
                request._ksw_fleet_post(Markup(_('<b>%(v)s</b> stay with %(d)s.%(n)s'))
                                        % {'v': names, 'd': request.department_id.name, 'n': note_part})
        # The receiving department's requests too: a transfer may settle them.
        (lines.request_id | moves.addition_line_id.request_id)._ksw_fleet_sync_state()
        return {'type': 'ir.actions.act_window_close'}
