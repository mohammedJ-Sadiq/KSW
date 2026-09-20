from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ItConsumable(models.Model):
    _name = 'it.consumable'
    _description = 'IT Consumable'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'name'
    _rec_names_search = ['name', 'code', 'compatible_with']

    # ------------------------------------------------------------------
    # Identification
    # ------------------------------------------------------------------
    code = fields.Char(
        string='Reference', required=True, copy=False, readonly=True, default='New',
    )
    name = fields.Char(
        required=True, tracking=True,
        help="e.g. 'HP 12A Black Toner Cartridge'.",
    )
    category_id = fields.Many2one(
        'it.consumable.category', string='Category', required=True, tracking=True,
    )
    brand = fields.Char()
    compatible_with = fields.Char(
        string='Compatible With',
        help="Printer/device model(s) this item fits, e.g. 'HP LaserJet P1102'.",
    )
    uom = fields.Selection([
        ('unit', 'Unit'),
        ('box', 'Box'),
        ('pack', 'Pack'),
    ], string='Unit of Measure', default='unit', required=True)
    active = fields.Boolean(default=True)
    color = fields.Integer(related='category_id.color', store=True, readonly=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    # ------------------------------------------------------------------
    # Stock
    # ------------------------------------------------------------------
    move_ids = fields.One2many('it.consumable.move', 'consumable_id', string='Stock Moves')
    move_count = fields.Integer(compute='_compute_move_count')
    qty_on_hand = fields.Integer(
        string='On Hand', compute='_compute_qty_on_hand', store=True,
        help="Current stock, derived from every Receive / Issue / "
             "Adjustment move recorded for this item.",
    )
    reorder_min = fields.Integer(
        string='Minimum Stock',
        help="Par level. When On Hand drops to this quantity or below, "
             "the item is flagged Low Stock and the IT Team gets a reminder.",
    )
    reorder_qty = fields.Integer(
        string='Reorder Quantity',
        help="Suggested quantity to buy when restocking this item.",
    )
    is_low_stock = fields.Boolean(
        compute='_compute_is_low_stock', search='_search_is_low_stock',
    )

    # ------------------------------------------------------------------
    # Procurement
    # ------------------------------------------------------------------
    location = fields.Char(help="Physical storage location, e.g. 'IT Store Room - Shelf 2'.")
    supplier_id = fields.Many2one('res.partner', string='Preferred Vendor')
    currency_id = fields.Many2one(related='company_id.currency_id')
    unit_cost = fields.Monetary(currency_field='currency_id')
    notes = fields.Text()

    _code_uniq = models.Constraint(
        'unique(code)',
        'This reference already exists.',
    )

    # ------------------------------------------------------------------
    # Compute
    # ------------------------------------------------------------------
    def _compute_display_name(self):
        for consumable in self:
            consumable.display_name = f'[{consumable.code}] {consumable.name}'

    def _compute_move_count(self):
        counts = self.env['it.consumable.move']._read_group(
            [('consumable_id', 'in', self.ids)], ['consumable_id'], ['__count'],
        )
        count_map = {consumable.id: count for consumable, count in counts}
        for consumable in self:
            consumable.move_count = count_map.get(consumable.id, 0)

    @api.depends('move_ids.quantity', 'move_ids.move_type')
    def _compute_qty_on_hand(self):
        sign_by_type = {'in': 1, 'out': -1, 'adjustment': 1}
        totals = self.env['it.consumable.move']._read_group(
            [('consumable_id', 'in', self.ids)],
            ['consumable_id', 'move_type'], ['quantity:sum'],
        )
        qty_map = dict.fromkeys(self.ids, 0)
        for consumable, move_type, qty_sum in totals:
            qty_map[consumable.id] = qty_map.get(consumable.id, 0) + sign_by_type[move_type] * qty_sum
        for consumable in self:
            consumable.qty_on_hand = qty_map.get(consumable.id, 0)

    @api.depends('qty_on_hand', 'reorder_min')
    def _compute_is_low_stock(self):
        for consumable in self:
            consumable.is_low_stock = consumable.qty_on_hand <= consumable.reorder_min

    def _search_is_low_stock(self, operator, value):
        if operator in ('in', 'not in'):
            wanted_low = (operator == 'in') == any(value)
        elif operator in ('=', '!='):
            wanted_low = (operator == '=') == bool(value)
        else:
            return NotImplemented
        low_stock_ids = self.search([]).filtered('is_low_stock').ids
        if wanted_low:
            return [('id', 'in', low_stock_ids)]
        return [('id', 'not in', low_stock_ids)]

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('code', 'New') == 'New':
                vals['code'] = self.env['ir.sequence'].next_by_code('it.consumable') or 'New'
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_open_issue_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Issue Stock'),
            'res_model': 'it.consumable.issue.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_consumable_id': self.id},
        }

    def action_open_receive_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Receive Stock'),
            'res_model': 'it.consumable.receive.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_consumable_id': self.id},
        }

    def action_generate_purchase_request(self):
        # Called from the Consumables list header button: with a selection,
        # request exactly those items; with none selected, default to
        # whatever is currently low on stock (the common case - see the
        # 30-day warranty reminder cron for the same "no selection" pattern
        # on it.asset).
        consumables = self
        if not consumables:
            consumables = self.search([('active', '=', True)]).filtered('is_low_stock')
        if not consumables:
            raise UserError(_('No consumables are currently low on stock.'))
        request = self.env['it.consumable.purchase.request'].create({
            'line_ids': [(0, 0, {
                'consumable_id': consumable.id,
                'qty_on_hand': consumable.qty_on_hand,
                'quantity': consumable.reorder_qty or max(
                    consumable.reorder_min - consumable.qty_on_hand, 1),
                'unit_cost': consumable.unit_cost,
            }) for consumable in consumables],
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _('Purchase Request'),
            'res_model': 'it.consumable.purchase.request',
            'view_mode': 'form',
            'res_id': request.id,
        }

    def action_view_moves(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Stock Moves'),
            'res_model': 'it.consumable.move',
            'view_mode': 'list,form',
            'domain': [('consumable_id', '=', self.id)],
            'context': {'default_consumable_id': self.id},
        }

    # ------------------------------------------------------------------
    # Cron
    # ------------------------------------------------------------------
    def _cron_low_stock_reminder(self):
        low_stock = self.search([('active', '=', True)]).filtered('is_low_stock')
        it_group = self.env.ref('KSW_helpdesk.group_helpdesk_agent', raise_if_not_found=False)
        users = it_group.users if it_group else self.env['res.users']
        for consumable in low_stock:
            for user in users:
                consumable.activity_schedule(
                    'mail.mail_activity_data_todo',
                    summary=_('Low stock: %s', consumable.name),
                    note=_(
                        '%(name)s (%(code)s) is at %(qty)d %(uom)s, at or '
                        'below the minimum stock of %(min)d. Suggested '
                        'reorder quantity: %(reorder)d.',
                        name=consumable.name, code=consumable.code,
                        qty=consumable.qty_on_hand, uom=consumable.uom,
                        min=consumable.reorder_min, reorder=consumable.reorder_qty,
                    ),
                    user_id=user.id,
                )
