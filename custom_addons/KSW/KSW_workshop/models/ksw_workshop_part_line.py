from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare


class KswWorkshopPartLine(models.Model):
    _name = 'ksw.workshop.part.line'
    _description = 'Workshop Request Part Line'
    _order = 'request_id, sequence, id'

    request_id = fields.Many2one(
        'ksw.workshop.request', required=True, ondelete='cascade', index=True,
    )
    sequence = fields.Integer(default=10)
    part_id = fields.Many2one('ksw.workshop.part', string='Part', required=True, ondelete='restrict')
    quantity = fields.Float(digits=(16, 3), required=True, default=1.0)
    unit_cost = fields.Float(
        string='Unit Cost', digits=(16, 4), compute='_compute_unit_cost', store=True, readonly=False,
    )
    returned_qty = fields.Float(
        string='Returned', digits=(16, 3), default=0.0, readonly=True, copy=False,
    )
    subtotal = fields.Float(compute='_compute_subtotal', store=True)
    qty_available = fields.Float(related='part_id.qty_on_hand', readonly=True)
    move_id = fields.Many2one('ksw.workshop.part.move', readonly=True, copy=False, ondelete='set null')
    return_move_id = fields.Many2one(
        'ksw.workshop.part.move', readonly=True, copy=False, ondelete='set null',
    )
    request_state = fields.Selection(related='request_id.state', store=True, index=True)
    note = fields.Char()

    _qty_positive = models.Constraint(
        'CHECK(quantity > 0)',
        'A part line quantity must be greater than zero.',
    )

    @api.depends('part_id')
    def _compute_unit_cost(self):
        for line in self:
            if line.part_id:
                line.unit_cost = line.part_id.standard_cost

    @api.depends('quantity', 'returned_qty', 'unit_cost')
    def _compute_subtotal(self):
        for line in self:
            line.subtotal = (line.quantity - line.returned_qty) * line.unit_cost

    @api.onchange('part_id', 'quantity')
    def _onchange_check_availability(self):
        for line in self:
            if line.part_id and line.quantity:
                already = line.move_id.quantity if line.move_id else 0.0
                if line.quantity - already > line.part_id.qty_on_hand:
                    return {'warning': {
                        'title': _('Insufficient stock'),
                        'message': _(
                            'Only %(avail).3f %(uom)s of %(part)s on hand.',
                            avail=line.part_id.qty_on_hand, uom=line.part_id.uom_name or '',
                            part=line.part_id.display_name,
                        ),
                    }}

    def _check_parent_editable(self, request=None):
        (request or self.mapped('request_id'))._check_report_edit_rights()

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            req_ids = {vals['request_id'] for vals in vals_list if vals.get('request_id')}
            if req_ids:
                self.env['ksw.workshop.request'].browse(req_ids)._check_report_edit_rights()
        # Savepoint: if _sync_move() fails (e.g. negative-stock ValidationError
        # from the movement it creates), the line itself must not be left
        # behind half-created — same all-or-nothing pattern as
        # scripts/import_history.py's per-row savepoint.
        with self.env.cr.savepoint():
            lines = super().create(vals_list)
            lines._sync_move()
        return lines

    def write(self, vals):
        self._check_parent_editable()
        with self.env.cr.savepoint():
            res = super().write(vals)
            if {'part_id', 'quantity', 'unit_cost'} & set(vals):
                self._sync_move()
        return res

    def unlink(self):
        self._check_parent_editable()
        moves = self.move_id | self.return_move_id
        res = super().unlink()
        moves.sudo().unlink()
        return res

    def _sync_move(self):
        """Create or update the single 'out' movement backing each line.

        sudo() here is for reach, not authority: _check_parent_editable()
        has already run in create()/write(), and the technician deliberately
        has no create/write access on ksw.workshop.part.move directly (see
        security), so this is the only path by which a consumption movement
        can be written.
        """
        Move = self.env['ksw.workshop.part.move'].sudo()
        for line in self:
            vals = {
                'part_id': line.part_id.id,
                'move_type': 'out',
                'quantity': line.quantity,
                'unit_cost': line.unit_cost,
                'request_id': line.request_id.id,
                'line_id': line.id,
            }
            if line.move_id:
                # sudo(): the move's own write() append-only guard blocks any
                # write on a line-owned move (by design, for direct RPC
                # edits) — this IS the legitimate line-owned update path, so
                # it must go through the same sudo'd recordset as create()
                # below, not through line.move_id (the caller's own env).
                Move.browse(line.move_id.id).write(vals)
            else:
                line.move_id = Move.create(dict(vals, date=fields.Datetime.now()))

    def action_return_to_stock(self):
        self.mapped('request_id')._check_manager()
        for line in self:
            remaining = line.quantity - line.returned_qty
            if float_compare(remaining, 0.0, precision_digits=3) <= 0:
                raise UserError(_('This part line has already been fully returned.'))
            move = self.env['ksw.workshop.part.move'].sudo().create({
                'part_id': line.part_id.id,
                'move_type': 'return',
                'quantity': remaining,
                'unit_cost': line.unit_cost,
                'request_id': line.request_id.id,
                'line_id': line.id,
                'date': fields.Datetime.now(),
                'reference': _('Return from %s', line.request_id.name),
            })
            line.sudo().write({'returned_qty': line.quantity, 'return_move_id': move.id})
