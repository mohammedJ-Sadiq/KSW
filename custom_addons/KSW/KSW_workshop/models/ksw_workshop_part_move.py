from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare


class KswWorkshopPartMove(models.Model):
    _name = 'ksw.workshop.part.move'
    _description = 'Workshop Part Stock Movement'
    _order = 'date desc, id desc'

    # A posted movement is history — only `note` may be edited afterward.
    # Consumption/return movements are additionally owned by their line:
    # edit the line, not the move, and _sync_move() rewrites this row.
    _EDITABLE_AFTER_POST = {'note'}

    name = fields.Char(default='New', readonly=True, copy=False)
    part_id = fields.Many2one(
        'ksw.workshop.part', string='Part', required=True, ondelete='restrict', index=True,
    )
    move_type = fields.Selection([
        ('in', 'Income'),
        ('out', 'Consumption'),
        ('return', 'Return'),
    ], required=True, index=True)
    quantity = fields.Float(string='Quantity', digits=(16, 3), required=True)
    unit_cost = fields.Float(string='Unit Cost', digits=(16, 4))
    amount = fields.Float(string='Amount', compute='_compute_amount', store=True)
    date = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    reference = fields.Char(help='Supplier invoice / delivery note / free text.')
    partner_id = fields.Many2one('res.partner', string='Supplier')
    request_id = fields.Many2one(
        'ksw.workshop.request', readonly=True, ondelete='set null', index=True,
    )
    # set null, never cascade: a DB-level cascade skips ksw.workshop.part.line's
    # Python unlink() and would leave qty_on_hand (a stored compute) stale.
    line_id = fields.Many2one(
        'ksw.workshop.part.line', readonly=True, ondelete='set null', copy=False,
    )
    user_id = fields.Many2one('res.users', default=lambda self: self.env.user, readonly=True)
    note = fields.Char()

    _qty_positive = models.Constraint(
        'CHECK(quantity > 0)',
        'A stock movement quantity must be greater than zero.',
    )

    @api.depends('quantity', 'unit_cost')
    def _compute_amount(self):
        for move in self:
            move.amount = move.quantity * move.unit_cost

    def _check_income_rights(self, vals_list=None):
        if self.env.su:
            return
        if vals_list is not None:
            types = [vals.get('move_type') for vals in vals_list]
        else:
            types = self.mapped('move_type')
        if 'in' in types and not self.env.user.has_group('KSW_workshop.group_workshop_manager'):
            raise UserError(_('Only the workshop manager can record stock income.'))

    @api.model_create_multi
    def create(self, vals_list):
        self._check_income_rights(vals_list)
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('ksw.workshop.part.move') or 'New'
        moves = super().create(vals_list)
        moves.filtered(lambda m: m.move_type == 'in' and m.unit_cost)._update_standard_cost()
        return moves

    def write(self, vals):
        if not self.env.su:
            self._check_income_rights()
            touched = set(vals.keys()) - self._EDITABLE_AFTER_POST
            if touched and any(move.line_id for move in self):
                raise UserError(_(
                    'A consumption movement is maintained from its repair-report '
                    'part line — edit the line instead.'
                ))
        res = super().write(vals)
        if {'unit_cost', 'move_type'} & set(vals):
            self.filtered(lambda m: m.move_type == 'in' and m.unit_cost)._update_standard_cost()
        return res

    def _update_standard_cost(self):
        for move in self:
            move.part_id.sudo().standard_cost = move.unit_cost

    @api.constrains('part_id', 'move_type', 'quantity')
    def _check_no_negative_stock(self):
        parts = self.mapped('part_id')
        qty = parts._get_live_qty()
        for part in parts:
            if float_compare(qty[part.id], 0.0, precision_digits=3) < 0:
                raise ValidationError(_(
                    'This movement would take %(part)s below zero on hand '
                    '(resulting quantity: %(qty).3f). Record the stock income first.',
                    part=part.display_name, qty=qty[part.id],
                ))
