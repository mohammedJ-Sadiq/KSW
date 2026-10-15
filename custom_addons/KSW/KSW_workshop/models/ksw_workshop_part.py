from collections import defaultdict

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
    # Workshop staff know a part by its name (فلتر زيت), not its item number.
    _rec_names_search = ['name', 'description']

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

    # What BAS paid before the workshop moved here: read once from the BAS
    # vouchers (net of VAT) and loaded as data, never recomputed. The price
    # note adds the issues recorded in Odoo on top, so it keeps moving.
    x_bas_last_price = fields.Float(string='BAS Last Price', readonly=True)
    x_bas_last_date = fields.Date(string='BAS Last Price Date', readonly=True)
    x_bas_avg_price = fields.Float(string='BAS Average Price', readonly=True)
    x_bas_qty = fields.Float(string='BAS Quantity Bought', readonly=True,
                             help="Units behind the BAS average, so Odoo issues weigh in correctly.")
    price_note = fields.Char(string='Price History', compute='_compute_price_note')

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

    @api.depends('x_bas_last_price', 'x_bas_last_date', 'x_bas_avg_price', 'x_bas_qty',
                 'line_ids.quantity', 'line_ids.unit_cost', 'line_ids.issue_date')
    def _compute_price_note(self):
        """«Last 100.00 (31/08/2026) · Avg 94.64 (447 pcs)», BAS history plus Odoo issues.

        sudo(): a technician sees only some requests, but the price of a part
        is the same whoever fitted it.
        """
        Line = self.env['ksw.workshop.part.line'].sudo()
        lines_by_part = defaultdict(list)
        for line in Line.search([('part_id', 'in', self.ids), ('unit_cost', '>', 0)],
                                order='issue_date desc, id desc'):
            lines_by_part[line.part_id.id].append(line)
        lang = self.env['res.lang']._get_data(code=self.env.lang or 'en_US')
        for part in self:
            lines = lines_by_part.get(part.id, [])
            last_price, last_date = part.x_bas_last_price, part.x_bas_last_date
            if lines and (not last_date or lines[0].issue_date >= last_date):
                last_price, last_date = lines[0].unit_cost, lines[0].issue_date
            qty = part.x_bas_qty + sum(l.quantity for l in lines)
            value = part.x_bas_avg_price * part.x_bas_qty + sum(l.quantity * l.unit_cost for l in lines)
            if not last_price and not qty:
                part.price_note = False
                continue
            note = self.env._('Last %(price)s', price=f'{last_price:,.2f}')
            if last_date:
                note += f' ({last_date.strftime(lang.date_format)})'
            if qty:
                note += ' · ' + self.env._('Avg %(price)s (%(qty)s pcs)',
                                           price=f'{value / qty:,.2f}', qty=f'{qty:g}')
            part.price_note = note

    def action_view_issues(self):
        """Every issue of this item, by month, each row opening its repair."""
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'KSW_workshop.action_ksw_workshop_report_parts')
        action['domain'] = [('part_id', '=', self.id)]
        action['context'] = {'group_by': ['issue_date:month']}
        action['display_name'] = self.display_name
        return action

    def action_view_note_mentions(self):
        """Repairs whose notes mention this item — the history before itemised parts."""
        self.ensure_one()
        text = self.description or self.name
        action = self.env['ir.actions.act_window']._for_xml_id(
            'KSW_workshop.action_ksw_workshop_report_date')
        action['domain'] = ['|', '|', ('repairs_parts', 'ilike', text),
                            ('work_statement', 'ilike', text), ('description', 'ilike', text)]
        action['context'] = {'group_by': ['create_date:year', 'vehicle_id']}
        action['display_name'] = self.env._('Repair notes mentioning “%s”', text)
        return action

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
