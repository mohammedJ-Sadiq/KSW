from odoo import api, fields, models


class KswWorkshopPart(models.Model):
    """A spare-part item, identified by its number on the purchase invoice.

    Deliberately NOT a stock item. There is no on-hand quantity, no income
    move, no negative-stock constraint and no valuation — parts pass straight
    through: the workshop buys them, fits them, and what is worth recording is
    the issue log, not a balance. That absence is the design, decided with the
    user (2026-09-08); a full ledger with all of the above was built in Aug
    2026 and removed the next day as too heavy for what the workshop needs
    (see the vault's "Workshop Parts Inventory (Postponed)"). Do not "fix"
    this by adding a quantity on hand.

    `name` is the item number the manager reads off the invoice he was holding
    when he bought the part — the workshop has no other item master, and asking
    for one would be asking him to invent it.
    """
    _name = 'ksw.workshop.part'
    _description = 'Workshop Spare Part Item'
    _order = 'name'

    name = fields.Char(
        string='Item No.', required=True,
        help="The item number as it appears on the purchase invoice for this part.")
    description = fields.Char()
    supplier_id = fields.Many2one(
        'res.partner', string='Supplier', domain="[('supplier_rank', '>', 0)]")
    invoice_ref = fields.Char(
        string='Purchase Invoice',
        help="Reference of the invoice this item number was taken from.")
    standard_cost = fields.Float(
        string='Unit Cost',
        help="Default unit cost proposed on a new issue line. Each line keeps its own "
             "snapshot, so changing this never rewrites what a past repair cost.")
    active = fields.Boolean(default=True)

    line_ids = fields.One2many('ksw.workshop.part.line', 'part_id', string='Issues')
    # Unstored on purpose: these are display figures on the item list. Anything
    # that needs to be grouped, sorted or measured goes through the Issued
    # Parts report, which pivots the lines themselves.
    issued_qty = fields.Float(string='Issued Qty', compute='_compute_issued')
    issued_value = fields.Float(string='Issued Value', compute='_compute_issued')

    _name_uniq = models.Constraint(
        'unique(name)',
        'An item with this number already exists.',
    )

    @api.depends('name', 'description')
    def _compute_display_name(self):
        for part in self:
            part.display_name = f'{part.name} — {part.description}' if part.description else part.name

    @api.depends('line_ids.quantity', 'line_ids.subtotal')
    def _compute_issued(self):
        grouped = {
            part.id: (qty, value)
            for part, qty, value in self.env['ksw.workshop.part.line'].sudo()._read_group(
                [('part_id', 'in', self.ids)],
                groupby=['part_id'],
                aggregates=['quantity:sum', 'subtotal:sum'],
            )
        }
        for part in self:
            qty, value = grouped.get(part.id, (0.0, 0.0))
            part.issued_qty = qty
            part.issued_value = value
