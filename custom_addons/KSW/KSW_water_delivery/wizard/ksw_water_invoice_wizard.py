from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

BILLING_GROUP = 'KSW_water_delivery.group_water_billing'
INVOICING_GROUP = 'account.group_account_invoice'


class KswWaterInvoiceWizard(models.TransientModel):
    """Month end, on one screen: a client's delivered notes for a period,
    summed by product, become one invoice.

    The wizard decides WHICH notes; `stock.picking._water_create_invoice` is
    what turns them into an invoice, so that a later "invoice every client
    for the month" run has the same rules by construction.
    """
    _name = 'ksw.water.invoice.wizard'
    _description = 'Invoice Water Delivery Notes'

    @api.model
    def _default_date_from(self):
        return fields.Date.context_today(self) + relativedelta(months=-1, day=1)

    @api.model
    def _default_date_to(self):
        return fields.Date.context_today(self) + relativedelta(day=1, days=-1)

    partner_id = fields.Many2one(
        'res.partner', string='Client', required=True,
        domain="[('id', 'in', allowed_partner_ids)]",
    )
    date_from = fields.Date(string='Delivered From', required=True,
                            default=_default_date_from)
    date_to = fields.Date(string='Delivered To', required=True,
                          default=_default_date_to)
    invoice_date = fields.Date(string='Invoice Date',
                               default=fields.Date.context_today)
    summary_only = fields.Boolean(
        string='Print One Line per Product',
        help='The printed invoice shows one line per product with its total. '
             'Every delivery note is still on the invoice underneath; this is '
             'core\'s "Hide Composition" on each product section, and can be '
             'switched on the invoice afterwards.',
    )

    # Relational + domain, not a dynamic selection: the options vary per
    # record (the period), see KSW gotcha #39.
    allowed_partner_ids = fields.Many2many(
        'res.partner', 'ksw_water_invoice_wizard_partner_rel',
        compute='_compute_allowed_partner_ids',
    )
    # Recomputed when the client or the period changes, and editable so a
    # disputed note can be taken out of this invoice and billed later.
    picking_ids = fields.Many2many(
        'stock.picking', 'ksw_water_invoice_wizard_picking_rel',
        string='Delivery Notes', compute='_compute_picking_ids',
        store=True, readonly=False,
    )
    line_ids = fields.One2many(
        'ksw.water.invoice.wizard.line', 'wizard_id', string='By Product',
        compute='_compute_line_ids', store=True,
    )
    company_currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id)
    amount_untaxed = fields.Monetary(
        compute='_compute_line_ids', store=True, currency_field='company_currency_id')
    amount_total = fields.Monetary(
        compute='_compute_line_ids', store=True, currency_field='company_currency_id')
    note_count = fields.Integer(compute='_compute_line_ids', store=True)

    # Notes in the period that are NOT going on this invoice, and why. Billing
    # needs to see these: a note silently missing from an invoice is the
    # unbilled revenue the whole module was built to stop.
    excluded_summary = fields.Text(compute='_compute_excluded_summary')

    # --- defaults from a selection on the notes list -----------------------
    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        ctx = self.env.context
        if ctx.get('active_model') == 'stock.picking' and ctx.get('active_ids'):
            notes = self.env['stock.picking'].browse(ctx['active_ids']).filtered_domain(
                self.env['stock.picking']._water_invoiceable_domain())
            clients = notes.partner_id.commercial_partner_id
            if len(clients) > 1:
                raise UserError(_(
                    'One invoice is for one client. The selected notes belong '
                    'to: %s', ', '.join(clients.mapped('display_name'))))
            if not notes:
                raise UserError(_('None of the selected notes is waiting to be invoiced.'))
            dates = notes.mapped('x_delivery_date')
            res.update({
                'partner_id': clients.id,
                'date_from': min(dates),
                'date_to': max(dates),
            })
        return res

    def _selected_note_ids(self):
        """Started from a selection on the notes list: that selection is the
        scope, not everything the client had in those dates. The onchange runs
        under the action's context, so the selection is still there."""
        ctx = self.env.context
        if ctx.get('active_model') == 'stock.picking':
            return set(ctx.get('active_ids') or [])
        return None

    # --- computes -----------------------------------------------------------
    def _period_notes(self):
        self.ensure_one()
        if not (self.partner_id and self.date_from and self.date_to):
            return self.env['stock.picking']
        Picking = self.env['stock.picking']
        return Picking.search(
            Picking._water_invoiceable_domain(self.date_from, self.date_to)
            + [('partner_id', 'child_of', self.partner_id._origin.id)],
            order='x_delivery_date, name',
        )

    @api.depends('date_from', 'date_to')
    def _compute_allowed_partner_ids(self):
        Picking = self.env['stock.picking']
        for wizard in self:
            groups = Picking._read_group(
                Picking._water_invoiceable_domain(wizard.date_from, wizard.date_to),
                ['partner_id'])
            wizard.allowed_partner_ids = self.env['res.partner'].union(
                *(p.commercial_partner_id for (p,) in groups))

    @api.depends('partner_id', 'date_from', 'date_to')
    def _compute_picking_ids(self):
        selection = self._selected_note_ids()
        for wizard in self:
            notes = wizard._period_notes()
            if selection is not None:
                notes = notes.filtered(lambda p: p.id in selection)
            wizard.picking_ids = notes

    @api.depends('picking_ids')
    def _compute_line_ids(self):
        Picking = self.env['stock.picking']
        for wizard in self:
            notes = wizard.picking_ids._origin
            groups = {}
            for note in notes:
                for line in note.sale_id.order_line.filtered(
                        lambda l: not l.display_type and l.qty_to_invoice > 0):
                    key = Picking._water_group_key(line)
                    group = groups.setdefault(key, {
                        'product_id': line.product_id.id,
                        'uom_id': line.product_uom_id.id,
                        'price_unit': line.price_unit,
                        'note_count': 0, 'quantity': 0.0,
                        'amount_untaxed': 0.0, 'amount_total': 0.0,
                    })
                    share = line.qty_to_invoice / (line.product_uom_qty or 1.0)
                    group['note_count'] += 1
                    group['quantity'] += line.qty_to_invoice
                    group['amount_untaxed'] += line.price_subtotal * share
                    group['amount_total'] += line.price_total * share
            rows = sorted(groups.values(), key=lambda g: (g['product_id'], g['price_unit']))
            wizard.line_ids = [(5, 0, 0)] + [(0, 0, row) for row in rows]
            wizard.amount_untaxed = sum(r['amount_untaxed'] for r in rows)
            wizard.amount_total = sum(r['amount_total'] for r in rows)
            wizard.note_count = len(notes)

    @api.depends('partner_id', 'date_from', 'date_to', 'picking_ids')
    def _compute_excluded_summary(self):
        Picking = self.env['stock.picking']
        for wizard in self:
            if not (wizard.partner_id and wizard.date_from and wizard.date_to):
                wizard.excluded_summary = False
                continue
            # A held note has a delivery date too: it was captured offline,
            # and the capture time is its delivery date.
            period = Picking.search([
                ('x_is_water_delivery', '=', True),
                ('partner_id', 'child_of', wizard.partner_id._origin.id),
                ('state', '!=', 'cancel'),
                ('x_delivery_date', '>=', wizard.date_from),
                ('x_delivery_date', '<=', wizard.date_to),
            ])
            invoiceable = wizard._period_notes()
            reasons = []
            held = period.filtered(lambda p: p.state != 'done')
            if held:
                reasons.append(_('%(n)s not validated yet (held for a dispatcher): %(names)s',
                                 n=len(held), names=', '.join(held.mapped('name'))))
            unsigned = period.filtered(
                lambda p: p.state == 'done' and p.x_signature_required and not p.x_signed_on)
            if unsigned:
                reasons.append(_('%(n)s not signed by the client: %(names)s',
                                 n=len(unsigned), names=', '.join(unsigned.mapped('name'))))
            removed = invoiceable - wizard.picking_ids._origin
            if removed:
                reasons.append(_('%(n)s taken off this invoice by you: %(names)s',
                                 n=len(removed), names=', '.join(removed.mapped('name'))))
            wizard.excluded_summary = '\n'.join(reasons) or False

    # --- authority ------------------------------------------------------------
    def _check_authority(self):
        """Raising an invoice is an accounting act, so it needs the accounting
        right as well as sight of the notes: the billing role alone reads
        notes and does not issue documents."""
        if self.env.su:
            return
        user = self.env.user
        if not user.has_group(BILLING_GROUP):
            raise UserError(_('Only water delivery billing can invoice delivery notes.'))
        if not user.has_group(INVOICING_GROUP):
            raise UserError(_(
                'Creating the invoice needs the Invoicing right '
                '(Accounting: Invoicing or above). Ask an administrator to add it.'))

    # --- the one button ---------------------------------------------------------
    def action_create_invoice(self):
        self.ensure_one()
        self._check_authority()
        notes = self.picking_ids
        if not notes:
            raise UserError(_(
                '%(client)s has no delivered notes waiting to be invoiced between '
                '%(start)s and %(end)s.', client=self.partner_id.display_name,
                start=self.date_from, end=self.date_to))
        invoice = notes._water_create_invoice(
            invoice_date=self.invoice_date, summary_only=self.summary_only)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Invoice'),
            'res_model': 'account.move',
            'res_id': invoice.id,
            'view_mode': 'form',
            'views': [(self.env.ref('account.view_move_form').id, 'form')],
            'target': 'current',
        }


class KswWaterInvoiceWizardLine(models.TransientModel):
    _name = 'ksw.water.invoice.wizard.line'
    _description = 'Invoice Water Delivery Notes: By Product'

    wizard_id = fields.Many2one('ksw.water.invoice.wizard', required=True, ondelete='cascade')
    product_id = fields.Many2one('product.product', string='Product', readonly=True)
    uom_id = fields.Many2one('uom.uom', string='Unit', readonly=True)
    note_count = fields.Integer(string='Notes', readonly=True)
    quantity = fields.Float(string='Quantity', digits='Product Unit', readonly=True)
    price_unit = fields.Float(string='Rate', digits='Product Price', readonly=True)
    currency_id = fields.Many2one(related='wizard_id.company_currency_id')
    amount_untaxed = fields.Monetary(string='Before Tax', readonly=True)
    amount_total = fields.Monetary(string='After Tax', readonly=True)
