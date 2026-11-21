from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

COUPLING_GROUP = 'KSW_fleet.group_tedder_coupling'
EDITOR_GROUP = 'KSW_fleet.group_tedder_editor'


class KswFleetCoupling(models.Model):
    """Which head (تريلا) pulled which tedder (تيدر), and when. The tank's
    licence is issued on the head, so this history says under whose papers a
    tedder ran on any day."""
    _name = 'ksw.fleet.coupling'
    _description = 'Tedder Coupling History'
    _order = 'date_from desc, id desc'

    tedder_id = fields.Many2one('ksw.fleet.vehicle', string='Tedder', required=True, index=True,
                                ondelete='cascade')
    head_id = fields.Many2one('ksw.fleet.vehicle', string='Head', required=True, index=True, ondelete='cascade')
    date_from = fields.Datetime(string='From', required=True, default=fields.Datetime.now)
    date_to = fields.Datetime(string='To', help='Empty while coupled.')
    user_id = fields.Many2one('res.users', string='Coupled By', required=True, default=lambda self: self.env.user)
    ended_by_id = fields.Many2one('res.users', string='Uncoupled By')
    note = fields.Char()

    def write(self, vals):
        if not self.env.su:
            raise UserError(_('Coupling history is written by coupling, not by hand.'))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise UserError(_('Coupling history is never deleted.'))
        return super().unlink()


class KswFleetVehicle(models.Model):
    _inherit = 'ksw.fleet.vehicle'

    # On a tedder: the head pulling it now. Set only through couple/uncouple.
    x_head_id = fields.Many2one('ksw.fleet.vehicle', string='Coupled To (Head)', index=True, readonly=True,
                                copy=False, ondelete='set null', domain=[('vehicle_type', '=', 'trailer')])
    x_tedder_ids = fields.One2many('ksw.fleet.vehicle', 'x_head_id', string='Tedders', readonly=True)
    # On a head: the tedder it pulls now.
    x_tedder_id = fields.Many2one('ksw.fleet.vehicle', string='Tedder', compute='_compute_tedder_id',
                                  compute_sudo=True)
    x_coupling_ids = fields.One2many('ksw.fleet.coupling', compute='_compute_coupling_ids',
                                     string='Coupling History', compute_sudo=True)
    x_can_couple = fields.Boolean(compute='_compute_can_couple')
    # Shown next to the tedder so the coupler sees which truck it is on.
    x_head_plate = fields.Char(related='x_head_id.plate_number', string='Head Plate')
    x_head_custodian_id = fields.Many2one(related='x_head_id.x_custodian_id', string='Head Driver')

    @api.depends('x_tedder_ids')
    def _compute_tedder_id(self):
        for vehicle in self:
            vehicle.x_tedder_id = vehicle.x_tedder_ids[:1]

    def _compute_coupling_ids(self):
        History = self.env['ksw.fleet.coupling']
        for vehicle in self:
            vehicle.x_coupling_ids = History.search(['|', ('tedder_id', '=', vehicle.id),
                                                     ('head_id', '=', vehicle.id)]) if vehicle.id else History

    @api.depends_context('uid')
    def _compute_can_couple(self):
        allowed = self._ksw_can_couple()
        for vehicle in self:
            vehicle.x_can_couple = allowed and vehicle.vehicle_type in ('tank', 'trailer')

    @api.model
    def _ksw_can_couple(self):
        return self.env.su or self.env.user.has_group(COUPLING_GROUP)

    @api.constrains('x_head_id', 'vehicle_type')
    def _check_coupling(self):
        for vehicle in self.filtered('x_head_id'):
            if vehicle.vehicle_type != 'tank':
                raise ValidationError(_('Only a tedder is coupled to a head.'))
            if vehicle.x_head_id.vehicle_type != 'trailer':
                raise ValidationError(_('%s is not a trailer head.', vehicle.x_head_id.name))
            if len(vehicle.x_head_id.x_tedder_ids) > 1:
                raise ValidationError(_('%s already pulls a tedder.', vehicle.x_head_id.name))

    x_coupled_since = fields.Datetime(compute='_compute_coupled_since', compute_sudo=True, string='Coupled Since')

    @api.depends('x_head_id', 'x_tedder_ids')
    def _compute_coupled_since(self):
        History = self.env['ksw.fleet.coupling']
        for vehicle in self:
            tedder = vehicle if vehicle.vehicle_type == 'tank' else vehicle.x_tedder_ids[:1]
            line = tedder and History.search([('tedder_id', '=', tedder.id), ('date_to', '=', False)], limit=1)
            vehicle.x_coupled_since = line.date_from if line else False

    def write(self, vals):
        if 'x_head_id' in vals and not self.env.context.get('ksw_coupling'):
            raise UserError(_('A tedder is coupled to its head with Couple to Head.'))
        self._ksw_check_tedder_data_rights(vals)
        self._ksw_check_tedder_editor_scope()
        return super().write(vals)

    # The tedder's own data, as on its form. Other processes write other
    # fields (custody, department, coupling) under their own guards.
    _TEDDER_DATA_FIELDS = {'name', 'client_id', 'vehicle_type', 'tank_number', 'plate_number', 'x_plate_en',
                           'x_registration_type', 'x_vin', 'brand', 'model', 'year', 'x_color', 'driver_id',
                           'driver_name', 'active', 'state'}

    x_can_edit_vehicle = fields.Boolean(compute='_compute_can_edit_vehicle')

    @api.depends_context('uid')
    @api.depends('vehicle_type')
    def _compute_can_edit_vehicle(self):
        sees_all = self.env.su or self.env.user.has_group('base.group_system')
        editor = self.env.user.has_group(EDITOR_GROUP)
        may_write = self.browse().has_access('write')
        for vehicle in self:
            vehicle.x_can_edit_vehicle = sees_all or (
                editor if vehicle.vehicle_type == 'tank' else may_write and self._ksw_writes_vehicles_otherwise())

    def _ksw_writes_vehicles_otherwise(self):
        """Can edit vehicles through a group other than «Edit Tedder Data»."""
        Access = self.env['ir.model.access'].sudo()
        other = Access.search([('model_id.model', '=', self._name), ('perm_write', '=', True),
                               ('group_id', '!=', self.env.ref(EDITOR_GROUP).id)]).group_id
        return bool(other & self.env.user.all_group_ids)

    def _ksw_check_tedder_data_rights(self, vals):
        """A tedder's data is «Edit Tedder Data»'s (user, 2026-11-21): an HR
        officer's general right to edit vehicles does not reach it."""
        if self.env.su or self.env.user.has_group('base.group_system') or self.env.user.has_group(EDITOR_GROUP):
            return
        if set(vals) & self._TEDDER_DATA_FIELDS and self.filtered(lambda v: v.vehicle_type == 'tank'):
            raise UserError(_('Editing a tedder\'s data needs the «Edit Tedder Data» permission.'))

    def _ksw_check_tedder_editor_scope(self):
        """«Edit Tedder Data» edits tedders only. Checked here, not with a
        record rule: a rule on that group would also narrow an HR officer who
        holds it, since Odoo ORs the rules of the groups that have one."""
        if self.env.su or not self.env.user.has_group(EDITOR_GROUP):
            return
        if self._ksw_writes_vehicles_otherwise():
            return  # can edit (non-tedder) vehicles anyway: HR officer, supervisor, administrator
        others = self.filtered(lambda v: v.vehicle_type != 'tank')
        if others:
            raise UserError(_('You may edit tedders only, not %s.', ', '.join(others.mapped('name'))))

    def _ksw_couple(self, head, note=False):
        """Couple this tedder to head, at once: whatever either was coupled
        to is uncoupled first, and the history records both."""
        self.ensure_one()
        if not self._ksw_can_couple():
            raise UserError(_('Coupling tedders to heads is reserved to the people given that permission.'))
        if self.vehicle_type != 'tank' or head.vehicle_type != 'trailer':
            raise UserError(_('Couple a tedder (تيدر) to a trailer head (تريلا).'))
        if self.x_head_id == head:
            return True
        now = fields.Datetime.now()
        History = self.env['ksw.fleet.coupling'].sudo()
        # The head's current tedder, and the tedder's current head, let go.
        (head.x_tedder_ids | self)._ksw_end_coupling(now)
        self.sudo().with_context(ksw_coupling=True).write({'x_head_id': head.id})
        History.create({'tedder_id': self.id, 'head_id': head.id, 'date_from': now, 'note': note})
        return True

    def _ksw_end_coupling(self, when=False):
        History = self.env['ksw.fleet.coupling'].sudo()
        for tedder in self.filtered('x_head_id'):
            History.search([('tedder_id', '=', tedder.id), ('date_to', '=', False)]).write(
                {'date_to': when or fields.Datetime.now(), 'ended_by_id': self.env.user.id})
            tedder.sudo().with_context(ksw_coupling=True).write({'x_head_id': False})

    def action_uncouple(self):
        if not self._ksw_can_couple():
            raise UserError(_('Coupling tedders to heads is reserved to the people given that permission.'))
        tedders = self.filtered(lambda v: v.vehicle_type == 'tank') | self.x_tedder_ids
        tedders._ksw_end_coupling()
        return True

    def action_open_couple_wizard(self):
        self.ensure_one()
        if not self._ksw_can_couple():
            raise UserError(_('Coupling tedders to heads is reserved to the people given that permission.'))
        ctx = {'default_tedder_id': self.id} if self.vehicle_type == 'tank' else {'default_head_id': self.id}
        return {'type': 'ir.actions.act_window', 'name': _('Couple Tedder to Head'),
                'res_model': 'ksw.fleet.couple.wizard', 'view_mode': 'form', 'target': 'new', 'context': ctx}


class KswFleetCoupleWizard(models.TransientModel):
    _name = 'ksw.fleet.couple.wizard'
    _description = 'Couple Tedder to Head'

    tedder_id = fields.Many2one('ksw.fleet.vehicle', string='Tedder', required=True,
                                domain=[('vehicle_type', '=', 'tank')])
    head_id = fields.Many2one('ksw.fleet.vehicle', string='Head', required=True,
                              domain=[('vehicle_type', '=', 'trailer')])
    note = fields.Char()
    # What the coupling replaces, so the person sees it before confirming.
    replaces = fields.Char(compute='_compute_replaces', compute_sudo=True)

    @api.depends('tedder_id', 'head_id')
    def _compute_replaces(self):
        for wizard in self:
            bits = []
            if wizard.tedder_id.x_head_id and wizard.tedder_id.x_head_id != wizard.head_id:
                bits.append(_('%(t)s leaves %(h)s', t=wizard.tedder_id.name, h=wizard.tedder_id.x_head_id.name))
            other = wizard.head_id.x_tedder_ids - wizard.tedder_id
            if other:
                bits.append(_('%(h)s drops %(t)s', h=wizard.head_id.name, t=other[:1].name))
            wizard.replaces = '; '.join(bits) or False

    def action_confirm(self):
        self.ensure_one()
        self.tedder_id._ksw_couple(self.head_id, self.note)
        return {'type': 'ir.actions.act_window_close'}
