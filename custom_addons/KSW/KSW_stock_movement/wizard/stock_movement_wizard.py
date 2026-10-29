"""Selection screen for the stock movement report.

The same shape as the BAS Item Movement screen: the prompt asks which stocks,
which items and which period, and how to group; Show reads Odoo's stock moves
for exactly that selection, stores the rows for this run and opens them as an
interactive grouped list (pivot and graph alongside).

Done movements come from ``stock.move.line`` (where the goods actually went,
down to the sub-location); not-yet-validated ones from ``stock.move``, which
has no lines of its own until it is reserved. Quantities are always in the
item's own unit, so rows of one item add up.
"""
from collections import defaultdict
from datetime import datetime, time, timedelta

import pytz

from odoo import _, fields, models
from odoo.exceptions import UserError

#: Past this the selection is too wide for one screen, and for the HTTP
#: worker's time limit.
MAX_LINES = 80000
_PENDING_STATES = ('waiting', 'confirmed', 'partially_available', 'assigned')

_GROUPINGS = [
    ('none', 'Nothing'),
    ('day', 'Day'),
    ('week', 'Week'),
    ('month', 'Month'),
    ('location', 'Location'),
    ('warehouse', 'Warehouse'),
    ('product', 'Item'),
    ('category', 'Item Category'),
    ('kind', 'In / Out'),
    ('partner', 'Partner'),
    ('picking_type', 'Operation Type'),
    ('document', 'Document'),
    ('state', 'Status'),
    ('invoice_status', 'Invoice Status'),
]
_GROUP_SPECS = {
    'day': 'date:day',
    'week': 'date:week',
    'month': 'date:month',
    'location': 'location_id',
    'warehouse': 'warehouse_id',
    'product': 'product_id',
    'category': 'categ_id',
    'kind': 'kind',
    'partner': 'partner_id',
    'picking_type': 'picking_type_id',
    'document': 'reference',
    'state': 'state',
    'invoice_status': 'invoice_status',
}


class KswStockMovementWizard(models.TransientModel):
    _name = 'ksw.stock.movement.wizard'
    _description = 'Stock Movement'
    _transient_max_hours = 12.0

    date_from = fields.Date(
        string='From', required=True,
        default=lambda self: fields.Date.context_today(self).replace(day=1))
    date_to = fields.Date(string='To', required=True, default=fields.Date.context_today)

    warehouse_ids = fields.Many2many(
        'stock.warehouse', string='Warehouses',
        help='Every stock location inside these warehouses.')
    location_ids = fields.Many2many(
        'stock.location', string='Stock Locations',
        domain="[('usage', '=', 'internal')]",
        help='Particular stock locations (and the locations inside them). '
             'Leave both this and Warehouses empty for every stock location.')
    product_ids = fields.Many2many(
        'product.product', string='Items', domain="[('type', '=', 'consu')]")
    categ_ids = fields.Many2many('product.category', string='Item Categories')
    partner_ids = fields.Many2many('res.partner', string='Partners')
    picking_type_ids = fields.Many2many('stock.picking.type', string='Operation Types')

    include_pending = fields.Boolean(
        string='Include Not Yet Validated',
        help='Also list transfers that are planned or ready but not validated. '
             'They are marked as such and are left out of the opening balance.')
    show_opening = fields.Boolean(
        string='Opening Balance Row', default=False,
        help='One row per item and location with what had moved in and out '
             'before the period, so grouping by item shows the closing balance.')

    layout = fields.Selection([
        ('collapsed', 'Grouped, collapsed (totals per group)'),
        ('expanded', 'Grouped, expanded (every line under its group)'),
        ('flat', 'All lines, no grouping'),
    ], string='Show', required=True, default='collapsed')
    group_by = fields.Selection(_GROUPINGS, string='Group By', required=True, default='product')
    group_by_2 = fields.Selection(_GROUPINGS, string='Then By', required=True, default='location')

    line_ids = fields.One2many('ksw.stock.movement.line', 'wizard_id')

    # ------------------------------------------------------------------
    def action_show(self):
        self.ensure_one()
        self._check_inputs()
        self.line_ids.unlink()
        self.env['ksw.stock.movement.line'].create(self._line_vals())
        group_by = [] if self.layout == 'flat' else [
            _GROUP_SPECS[key] for key in (self.group_by, self.group_by_2) if key != 'none']
        list_view = self.env.ref('KSW_stock_movement.view_stock_movement_line_list%s' % (
            '_expanded' if self.layout == 'expanded' else ''))
        return {
            'type': 'ir.actions.act_window',
            'name': self._title(),
            'res_model': 'ksw.stock.movement.line',
            'views': [(list_view.id, 'list'), (False, 'pivot'), (False, 'graph')],
            'search_view_id': [self.env.ref(
                'KSW_stock_movement.view_stock_movement_line_search').id, 'search'],
            'domain': [('wizard_id', '=', self.id)],
            'context': {'group_by': group_by, 'create': False, 'edit': False, 'delete': False},
            'target': 'current',
        }

    def _check_inputs(self):
        if self.date_from > self.date_to:
            raise UserError(_('The start date is after the end date.'))
        if self.layout == 'flat':
            self.group_by = self.group_by_2 = 'none'
        elif self.group_by == 'none':
            raise UserError(_('Choose what to group by, or show all lines without grouping.'))

    def _title(self):
        return _('Stock Movement %(from)s → %(to)s',
                 **{'from': self.date_from, 'to': self.date_to})

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------
    def _scope_locations(self):
        """Every internal location the report is about, children included."""
        Location = self.env['stock.location']
        roots = self.location_ids | self.warehouse_ids.view_location_id
        domain = [('usage', '=', 'internal')]
        if roots:
            domain.append(('id', 'child_of', roots.ids))
        else:
            domain.append(('company_id', 'in', self.env.companies.ids))
        return Location.search(domain)

    def _bounds(self):
        """The period as UTC datetimes: midnight to midnight in the user's zone."""
        tz = pytz.timezone(self.env.user.tz or 'Asia/Riyadh')

        def utc(day):
            return tz.localize(datetime.combine(day, time.min)).astimezone(
                pytz.utc).replace(tzinfo=None)
        return utc(self.date_from), utc(self.date_to + timedelta(days=1)), tz

    def _filter_domain(self, on_line=False):
        """Item / partner / operation filters, for move lines (``on_line``)
        or moves. A delivery keeps its customer on the transfer, a move made
        outside a transfer on the move itself, so the partner looks at both."""
        move = 'move_id.' if on_line else ''
        domain = []
        if self.product_ids:
            domain.append(('product_id', 'in', self.product_ids.ids))
        if self.categ_ids:
            domain.append(('product_id.categ_id', 'child_of', self.categ_ids.ids))
        if self.partner_ids:
            domain += ['|', ('picking_id.partner_id', 'child_of', self.partner_ids.ids),
                       (move + 'partner_id', 'child_of', self.partner_ids.ids)]
        if self.picking_type_ids:
            domain.append(('picking_type_id', 'in', self.picking_type_ids.ids))
        return domain

    # ------------------------------------------------------------------
    # Rows
    # ------------------------------------------------------------------
    def _line_vals(self):
        scope = self._scope_locations()
        if not scope:
            raise UserError(_('No stock location matches this selection.'))
        start, end, tz = self._bounds()
        MoveLine = self.env['stock.move.line'].sudo()
        Move = self.env['stock.move'].sudo()

        touches = ['|', ('location_id', 'in', scope.ids), ('location_dest_id', 'in', scope.ids)]
        done_domain = [('state', '=', 'done'), ('date', '>=', start), ('date', '<', end),
                       *touches, *self._filter_domain(on_line=True)]
        pending_domain = [('state', 'in', _PENDING_STATES), ('date', '<', end),
                          *touches, *self._filter_domain()] if self.include_pending else None

        count = MoveLine.search_count(done_domain) + (
            Move.search_count(pending_domain) if pending_domain else 0)
        if count > MAX_LINES:
            raise UserError(_(
                '%(count)s movements match this selection, more than %(max)s can be '
                'shown at once. Choose a shorter period, fewer stocks or particular '
                'items.', count=count, max=MAX_LINES))

        scope_ids = set(scope.ids)
        vals_list = []
        if self.show_opening:
            vals_list += self._opening_vals(scope, start)

        for ml in MoveLine.search(done_domain, order='date, id'):
            move = ml.move_id
            vals_list += self._side_vals(
                scope_ids, ml.location_id, ml.location_dest_id, ml.product_id,
                ml.quantity_product_uom, ml.date, tz, 'done', move, ml.picking_id,
                ml.picking_partner_id or move.partner_id)

        if pending_domain:
            for move in Move.search(pending_domain, order='date, id'):
                vals_list += self._side_vals(
                    scope_ids, move.location_id, move.location_dest_id, move.product_id,
                    move.product_qty, move.date, tz, 'pending', move, move.picking_id,
                    move.picking_id.partner_id or move.partner_id)
        return vals_list

    def _side_vals(self, scope_ids, src, dest, product, qty, when, tz, state,
                   move, picking, partner):
        """An Out row if it left a chosen stock, an In row if it reached one."""
        common = {
            'wizard_id': self.id,
            'date': pytz.utc.localize(when).astimezone(tz).date() if when else False,
            'state': state,
            'reference': move.reference or picking.name or False,
            'picking_id': picking.id or False,
            'picking_type_id': move.picking_type_id.id or False,
            'origin': move.origin or False,
            'partner_id': partner.id or False,
            'product_id': product.id,
            'categ_id': product.categ_id.id,
            'uom_id': product.uom_id.id,
            'invoice_status': self._invoice_status(move),
        }
        rows = []
        if src.id in scope_ids:
            rows.append(dict(common, kind='out', location_id=src.id,
                             other_location_id=dest.id,
                             warehouse_id=src.warehouse_id.id or False,
                             qty_in=0.0, qty_out=qty, qty_net=-qty))
        if dest.id in scope_ids:
            rows.append(dict(common, kind='in', location_id=dest.id,
                             other_location_id=src.id,
                             warehouse_id=dest.warehouse_id.id or False,
                             qty_in=qty, qty_out=0.0, qty_net=qty))
        return rows

    def _invoice_status(self, move):
        # sale_stock is not a dependency: the report runs without Sales, and
        # simply has nothing to say about invoicing there.
        line = move['sale_line_id'] if 'sale_line_id' in move._fields else False
        return line.invoice_status if line else False

    def _opening_vals(self, scope, start):
        """Per item and location: done in minus done out before the period."""
        MoveLine = self.env['stock.move.line'].sudo()
        base = [('state', '=', 'done'), ('date', '<', start), *self._filter_domain(on_line=True)]
        balance = defaultdict(float)
        for location, product, qty in MoveLine._read_group(
                base + [('location_dest_id', 'in', scope.ids)],
                ['location_dest_id', 'product_id'], ['quantity_product_uom:sum']):
            balance[location, product] += qty
        for location, product, qty in MoveLine._read_group(
                base + [('location_id', 'in', scope.ids)],
                ['location_id', 'product_id'], ['quantity_product_uom:sum']):
            balance[location, product] -= qty

        rows = []
        for (location, product), qty in balance.items():
            if abs(round(qty, 6)) < 1e-6:
                continue
            rows.append({
                'wizard_id': self.id,
                'sequence': 0,
                'date': self.date_from,
                'kind': 'opening',
                'state': 'done',
                'reference': _('Opening Balance'),
                'location_id': location.id,
                'warehouse_id': location.warehouse_id.id or False,
                'product_id': product.id,
                'categ_id': product.categ_id.id,
                'uom_id': product.uom_id.id,
                'qty_in': max(qty, 0.0),
                'qty_out': max(-qty, 0.0),
                'qty_net': qty,
            })
        return rows
