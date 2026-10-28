"""One side of one stock movement, as the Stock Movement screen lists it.

A snapshot taken by the selection screen (``ksw.stock.movement.wizard``) for
one run: Show reads the moves for exactly that selection, and the rows go away
with the run. Nothing else in Odoo reads them.

A movement between two of the chosen stocks is two rows -- Out at the one it
left, In at the one it reached -- so grouping by location gives each stock its
own balance and the Net column over all of them nets the transfer to zero.
"""
from odoo import fields, models


class KswStockMovementLine(models.TransientModel):
    _name = 'ksw.stock.movement.line'
    _description = 'Stock Movement Line'
    _order = 'date, sequence, id'
    # A report can stay open on someone's screen for a working day.
    _transient_max_hours = 12.0

    wizard_id = fields.Many2one(
        'ksw.stock.movement.wizard', required=True, ondelete='cascade', index=True)
    # Opening rows sort before the movements of their first day.
    sequence = fields.Integer(default=10)

    date = fields.Date(string='Date', readonly=True)
    kind = fields.Selection([
        ('opening', 'Opening Balance'),
        ('in', 'In'),
        ('out', 'Out'),
    ], string='Movement', readonly=True)
    state = fields.Selection([
        ('done', 'Done'),
        ('pending', 'Not Yet Validated'),
    ], string='Status', readonly=True)

    reference = fields.Char(string='Document', readonly=True)
    picking_id = fields.Many2one('stock.picking', string='Transfer', readonly=True)
    picking_type_id = fields.Many2one('stock.picking.type', string='Operation Type', readonly=True)
    origin = fields.Char(string='Source Document', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Partner', readonly=True)

    location_id = fields.Many2one(
        'stock.location', string='Location', readonly=True,
        help='The chosen stock this row moved into or out of.')
    other_location_id = fields.Many2one(
        'stock.location', string='From / To', readonly=True,
        help='Where it came from (In) or went to (Out).')
    warehouse_id = fields.Many2one('stock.warehouse', string='Warehouse', readonly=True)

    product_id = fields.Many2one('product.product', string='Item', readonly=True)
    categ_id = fields.Many2one('product.category', string='Item Category', readonly=True)
    uom_id = fields.Many2one('uom.uom', string='Unit', readonly=True)
    qty_in = fields.Float(string='In', readonly=True, aggregator='sum',
                          digits='Product Unit')
    qty_out = fields.Float(string='Out', readonly=True, aggregator='sum',
                           digits='Product Unit')
    qty_net = fields.Float(
        string='Net', readonly=True, aggregator='sum', digits='Product Unit',
        help='In minus Out. Summed per item with the opening row, it is the '
             'balance at the end of the period.')

    invoice_status = fields.Selection([
        ('invoiced', 'Invoiced'),
        ('to invoice', 'To Invoice'),
        ('upselling', 'Upselling Opportunity'),
        ('no', 'Nothing to Invoice'),
    ], string='Invoice Status', readonly=True,
        help='For a delivery made from a sales order: whether that order line '
             'has been invoiced yet.')
