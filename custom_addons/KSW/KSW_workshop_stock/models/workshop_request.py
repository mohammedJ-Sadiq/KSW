from collections import defaultdict

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare

from odoo.addons.KSW_workshop.models.ksw_workshop_request import KswWorkshopRequest as _Base


class KswWorkshopRequest(models.Model):
    _inherit = 'ksw.workshop.request'

    # Same rule as the parts themselves: technician or manager, In Progress only.
    _REPORT_FIELDS = _Base._REPORT_FIELDS | {'x_warehouse_id'}

    x_warehouse_id = fields.Many2one(
        'stock.warehouse', string='Parts Warehouse',
        default=lambda self: self._default_parts_warehouse(),
        help="Warehouse the spare parts are issued from.")
    x_cost_centre_id = fields.Many2one(
        related='vehicle_id.x_analytic_account_id', string='Vehicle Cost Centre')
    x_issue_picking_id = fields.Many2one(
        'stock.picking', string='Stock Issue', readonly=True, copy=False)

    @api.model
    def _default_parts_warehouse(self):
        """BAS warehouse 15, «الكفرات وقطع الغيار», unless configured otherwise."""
        param = self.env['ir.config_parameter'].sudo().get_param('ksw_workshop_stock.warehouse_id')
        Warehouse = self.env['stock.warehouse'].sudo()
        if param and Warehouse.browse(int(param)).exists():
            return int(param)
        return Warehouse.search([('x_bas_code', '=', '15'), ('x_bas_kind', '=', 'warehouse')], limit=1).id

    def action_complete(self):
        # The manager check comes first: issuing stock is irreversible, and
        # super() would only refuse a technician after the stock had moved.
        self._check_manager()
        for request in self:
            request._ksw_issue_parts()
        return super().action_complete()

    def _ksw_issue_parts(self):
        """Issue this repair's stock lines in one Stock Issue, and price them.

        sudo() for the stock and accounting writes: the workshop manager is
        not an inventory or accounting user, and the caller's right to complete
        this request has already been checked.
        """
        self.ensure_one()
        lines = self.part_line_ids.filtered('product_id')
        if not lines or self.x_issue_picking_id:
            return
        if self.state != 'in_progress':
            raise UserError(self.env._('Only an in-progress repair can issue parts.'))
        warehouse = self.x_warehouse_id.sudo()
        if not warehouse:
            raise UserError(self.env._('Choose the warehouse the spare parts come from.'))
        cost_centre = self.x_cost_centre_id
        if not cost_centre:
            raise UserError(self.env._(
                "Vehicle %(vehicle)s has no cost centre, so its spare parts can't be charged. "
                "Set one on the vehicle first.", vehicle=self.vehicle_id.display_name or self.x_cash_vehicle_number or '-'))
        issue_type = self.env['stock.picking.type'].sudo().search(
            [('warehouse_id', '=', warehouse.id), ('x_ksw_issue', '=', True)], limit=1)
        if not issue_type:
            raise UserError(self.env._('Warehouse %s has no Stock Issue operation.', warehouse.display_name))

        need = defaultdict(float)
        for line in lines:
            need[line.product_id] += line.quantity
        short = []
        for product, qty in need.items():
            available = product.sudo().with_context(location=warehouse.lot_stock_id.id).free_qty
            if float_compare(available, qty, precision_rounding=product.uom_id.rounding) < 0:
                short.append(f"{product.display_name}: {qty:g} needed, {available:g} in {warehouse.code}")
        if short:
            raise UserError(self.env._(
                "Not enough stock to complete %(name)s:\n%(lines)s", name=self.name, lines="\n".join(short)))

        consumption = issue_type.default_location_dest_id
        picking = self.env['stock.picking'].sudo().create({
            'picking_type_id': issue_type.id,
            'location_id': warehouse.lot_stock_id.id,
            'location_dest_id': consumption.id,
            'origin': self.name,
            'move_ids': [(0, 0, {
                'product_id': line.product_id.id,
                'product_uom_qty': line.quantity,
                'product_uom': line.product_id.uom_id.id,
                'location_id': warehouse.lot_stock_id.id,
                'location_dest_id': consumption.id,
                'analytic_distribution': {str(cost_centre.id): 100},
                'description_picking': line.description or line.product_id.display_name,
            }) for line in lines],
        })
        picking.action_confirm()
        picking.action_assign()
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        picking.button_validate()
        if picking.state != 'done':
            raise UserError(self.env._('The stock issue for %s could not be completed.', self.name))
        # Price each line at what was actually issued (weighted average). Moves
        # are matched by item, not by position: confirming a picking merges
        # two lines of the same item into one move.
        move_by_product = {move.product_id: move for move in picking.move_ids}
        for line in lines:
            move = move_by_product[line.product_id]
            line.sudo().write({'x_move_id': move.id,
                               'unit_cost': (move.value / move.quantity) if move.quantity else 0.0})
        self.sudo().x_issue_picking_id = picking

    def action_view_issue_picking(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'res_model': 'stock.picking',
            'res_id': self.x_issue_picking_id.id, 'view_mode': 'form',
        }


class KswWorkshopPartLine(models.Model):
    _inherit = 'ksw.workshop.part.line'

    # A line is either a stock item (issued at completion) or, on old data, a
    # pass-through item; the pass-through list is hidden in the UI.
    part_id = fields.Many2one(required=False)
    product_id = fields.Many2one(
        'product.product', string='Item', index=True,
        domain="[('is_storable', '=', True)]")
    x_available_qty = fields.Float(
        string='On Hand', compute='_compute_available_qty', digits='Product Unit',
        help="Free quantity in the repair's parts warehouse right now.")
    x_move_id = fields.Many2one('stock.move', string='Stock Move', readonly=True, copy=False)

    @api.depends('product_id', 'request_id.x_warehouse_id')
    def _compute_available_qty(self):
        for line in self:
            wh = line.request_id.x_warehouse_id.sudo()
            line.x_available_qty = line.product_id.sudo().with_context(
                location=wh.lot_stock_id.id).free_qty if line.product_id and wh else 0.0

    @api.constrains('part_id', 'product_id')
    def _check_item(self):
        for line in self:
            if not line.part_id and not line.product_id:
                raise ValidationError(self.env._('Choose the spare part item.'))

    @api.onchange('product_id')
    def _onchange_product_id(self):
        """Snapshots, as for pass-through items: estimate now, actual at issue."""
        for line in self:
            if line.product_id:
                if not line.description:
                    line.description = line.product_id.display_name
                if not line.unit_cost:
                    line.unit_cost = line.product_id.sudo().standard_price

    @api.model_create_multi
    def create(self, vals_list):
        Product = self.env['product.product'].sudo()
        for vals in vals_list:
            if vals.get('product_id') and not vals.get('part_id'):
                product = Product.browse(vals['product_id'])
                vals.setdefault('description', product.display_name)
                if not vals.get('unit_cost'):
                    vals['unit_cost'] = product.standard_price
        return super().create(vals_list)

    def write(self, vals):
        if self.filtered('x_move_id') and set(vals) - {'x_move_id', 'unit_cost'}:
            raise UserError(self.env._('Parts already issued from stock cannot be changed.'))
        return super().write(vals)

    def unlink(self):
        if self.filtered('x_move_id'):
            raise UserError(self.env._('Parts already issued from stock cannot be removed.'))
        return super().unlink()
