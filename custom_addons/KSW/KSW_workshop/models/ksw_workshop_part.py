from odoo import _, api, fields, models
from odoo.exceptions import UserError


class KswWorkshopPart(models.Model):
    _name = 'ksw.workshop.part'
    _description = 'Workshop Spare Part'
    _order = 'name'

    name = fields.Char(required=True)
    code = fields.Char(string='Part Number')
    category = fields.Selection([
        ('mechanical', 'Mechanical'),
        ('bodywork', 'Bodywork / Welding'),
        ('oil_filters', 'Oil & Filters'),
        ('electrical', 'Electrical'),
        ('other', 'Other'),
    ], default='other', required=True)
    uom_name = fields.Char(string='Unit', default='Unit')

    standard_cost = fields.Float(
        string='Standard Cost', digits=(16, 4),
        help='Unit cost from the most recent stock income. Used as the default '
             'cost when a part is consumed on a workshop request.',
    )
    min_qty = fields.Float(string='Reorder Threshold', digits=(16, 3))
    qty_on_hand = fields.Float(
        string='On Hand', digits=(16, 3), compute='_compute_qty_on_hand', store=True, readonly=True,
    )
    is_low_stock = fields.Boolean(compute='_compute_is_low_stock', store=True)
    inventory_value = fields.Float(
        string='Inventory Value', digits=(16, 2), compute='_compute_inventory_value', store=True,
        help='On Hand × Standard Cost.',
    )

    move_ids = fields.One2many('ksw.workshop.part.move', 'part_id', string='Stock Movements')

    state = fields.Selection([
        ('draft', 'Draft'),
        ('confirmed', 'Confirmed'),
    ], default='confirmed', copy=False, required=True)

    active = fields.Boolean(default=True)
    note = fields.Text()

    _code_uniq = models.Constraint(
        'unique(code)',
        'A part with this part number already exists.',
    )

    def _compute_display_name(self):
        for part in self:
            part.display_name = f'[{part.code}] {part.name}' if part.code else part.name

    def _get_live_qty(self):
        """{part.id: qty on hand} read straight from the move table.

        Shared by _compute_qty_on_hand and ksw.workshop.part.move's
        negative-stock constraint — a constrains handler cannot safely rely
        on this record's own stored qty_on_hand, since that recompute may
        not have flushed yet when the constraint runs.
        """
        sign = {'in': 1.0, 'out': -1.0, 'return': 1.0}
        qty = dict.fromkeys(self.ids, 0.0)
        groups = self.env['ksw.workshop.part.move'].sudo()._read_group(
            [('part_id', 'in', self.ids)], ['part_id', 'move_type'], ['quantity:sum'],
        )
        for part, move_type, total in groups:
            qty[part.id] += sign[move_type] * total
        return qty

    @api.depends('move_ids.quantity', 'move_ids.move_type', 'move_ids.part_id')
    def _compute_qty_on_hand(self):
        qty = self._get_live_qty()
        for part in self:
            part.qty_on_hand = qty.get(part.id, 0.0)

    @api.depends('qty_on_hand', 'min_qty')
    def _compute_is_low_stock(self):
        for part in self:
            part.is_low_stock = bool(part.min_qty) and part.qty_on_hand <= part.min_qty

    @api.depends('qty_on_hand', 'standard_cost')
    def _compute_inventory_value(self):
        for part in self:
            part.inventory_value = part.qty_on_hand * part.standard_cost

    @api.model_create_multi
    def create(self, vals_list):
        is_manager = self.env.su or self.env.user.has_group('KSW_workshop.group_workshop_manager')
        for vals in vals_list:
            if not is_manager:
                vals['state'] = 'draft'
            elif not vals.get('state'):
                vals['state'] = 'confirmed'
        return super().create(vals_list)

    def action_confirm(self):
        if not self.env.su and not self.env.user.has_group('KSW_workshop.group_workshop_manager'):
            raise UserError(_('Only the workshop manager can confirm a draft part.'))
        for part in self:
            if part.state != 'draft':
                raise UserError(_('Only draft parts can be confirmed.'))
        self.write({'state': 'confirmed'})

    def unlink(self):
        if any(part.move_ids for part in self):
            raise UserError(_(
                'A part with stock movements cannot be deleted. Archive it instead.'
            ))
        return super().unlink()
