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
        ('transfer', 'Give them to a department that asked'),
        ('holding', 'Send to workshop and hold'),
        ('keep', 'Keep them where they are'),
    ], required=True, default='transfer')
    with_driver = fields.Boolean(compute='_compute_sources')
    # A vehicle goes only to a department that asked: the GM picks the open
    # request it fills, not a department.
    addition_line_id = fields.Many2one(
        'ksw.fleet.addition.line', string='For Request',
        domain="[('id', 'in', demand_line_ids)]")
    vehicle_type = fields.Char(compute='_compute_sources')
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
            kinds = set(wizard.release_line_ids.mapped('vehicle_type'))
            wizard.vehicle_type = kinds.pop() if len(kinds) == 1 else False
            wizard.with_driver = all(wizard.release_line_ids.mapped('x_with_driver')) and bool(wizard.release_line_ids)
            wizard.vehicle_types = ', '.join(sorted({
                types.get(t, '') for t in wizard.release_line_ids.mapped('vehicle_type') if t}))

    @api.depends('release_line_ids', 'release_line_ids.x_with_driver')
    def _compute_demand_line_ids(self):
        for wizard in self:
            # The requests these vehicles can fill: same type, a need that
            # accepts them (with or without their driver), room left.
            demand = self.env['ksw.fleet.addition.line'].search([
                ('vehicle_type', 'in', wizard.release_line_ids.mapped('vehicle_type')),
                ('open_quantity', '>', 0), ('request_id.state', '=', 'submitted'),
                ('department_id', 'not in', wizard.release_line_ids.department_id.ids)])
            drivers = set(wizard.release_line_ids.mapped('x_with_driver'))
            wizard.demand_line_ids = demand.filtered(lambda l: all(l._ksw_accepts(d) for d in drivers))

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
        if self.decision == 'keep' and not self.note:
            raise UserError(_('Write why they are kept: the department asked to give them up.'))
        moves = self.env['ksw.fleet.move']
        note_part = Markup('<br/><i>%s</i>') % self.note if self.note else Markup('')
        if self.decision == 'transfer':
            if len(set(lines.mapped('x_with_driver'))) > 1:
                raise UserError(_('Decide the vehicles released with their drivers separately from '
                                  'the ones released alone: they fill different needs.'))
            lines._ksw_check_fills(self.addition_line_id)
            moves |= lines.with_context(ksw_fleet_batch=True)._ksw_execute_transfer(
                self.addition_line_id.sudo(), self.note)
        elif self.decision == 'holding':
            if lines.filtered('x_with_driver'):
                raise UserError(_('A vehicle released with its driver goes to a department that asked '
                                  'for one, or is kept; not to the workshop: %s.',
                                  ', '.join(lines.filtered('x_with_driver').vehicle_id.mapped('name'))))
            for line in lines.with_context(ksw_fleet_batch=True):
                moves |= line.vehicle_id._ksw_fleet_send_to_holding(self.workshop_description, self.note)
        else:
            lines.write({'decision': 'keep', 'decision_note': self.note,
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
