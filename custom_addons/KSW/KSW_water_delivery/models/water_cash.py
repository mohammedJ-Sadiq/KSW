"""Cash sales at the branch desk (Oct 2026).

BAS issues a cash delivery note against the branch till (1201...) instead of
a customer. Here the dispatcher does it on a dispatch ticked **Cash Sale**:

* the client is the branch's **cash customer** (one contact per branch, its
  own pricelist = the branch's cash prices), with the buyer's name and phone
  optional on the dispatch; a known customer may be picked instead;
* the money is taken **at the desk, before delivery**: Dispatch posts a
  customer payment into the branch's **cash till** journal;
* the driver delivers it like any dispatch; once the note is validated the
  sale is invoiced at once in the **cash sales** journal and matched to that
  payment, so month-end billing never sees it again;
* sent back to draft or cancelled before delivery, the payment is cancelled
  (the cash is handed back) and a new one is taken on the next Dispatch.

A cash dispatch works on any branch, whatever its issue mode: cash is always
taken at the desk.
"""
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class StockPickingType(models.Model):
    _inherit = 'stock.picking.type'

    x_cash_partner_id = fields.Many2one(
        'res.partner', string='Cash Customer', copy=False,
        help='The contact this branch\'s cash sales are made out to. Its '
             'pricelist holds the branch\'s cash prices (Client Rates).')
    x_cash_journal_id = fields.Many2one(
        'account.journal', string='Cash Till', copy=False,
        domain="[('type', '=', 'cash')]",
        help='Where the dispatcher\'s cash goes: the branch till.')
    x_cash_sale_journal_id = fields.Many2one(
        'account.journal', string='Cash Sales Journal', copy=False,
        domain="[('type', '=', 'sale')]",
        help='The journal cash sales are invoiced in. Empty uses the '
             'company\'s Cash Sales (CSAL) journal.')

    def action_create_cash_customer(self):
        """Give the branch its cash customer, with its own (empty) pricelist.
        The cash prices are then entered in Client Rates for that customer."""
        if not self.env.user.has_group('base.group_system'):
            raise UserError(_('Only the administrator sets up cash sales.'))
        Pricing = self.env['ksw.water.pricing']
        for branch in self.filtered(lambda b: not b.x_cash_partner_id):
            partner = self.env['res.partner'].sudo().create({
                'name': _('Cash Customer - %s', branch.name),
                'is_company': True,
                'customer_rank': 1,
                'company_id': branch.company_id.id or False,
            })
            Pricing.with_company(branch.company_id or self.env.company) \
                ._ensure_client_pricelist(partner)
            branch.sudo().x_cash_partner_id = partner
        return True

    def _water_till_problem(self):
        """Why the till cannot take a receipt, or False. A cash journal whose
        Manual Payment line has no account falls back to the company's
        outstanding-receipts account, which KSW retired (101003): the receipt
        must post straight to the till's own account, like the C00xx tills."""
        self.ensure_one()
        till = self.sudo().x_cash_journal_id
        if till and not till.inbound_payment_method_line_ids.filtered('payment_account_id'):
            return _('The till %(till)s does not say where receipts post. In Accounting ▸ '
                     'Configuration ▸ Journals ▸ %(till)s ▸ Incoming Payments, set the '
                     'Manual Payment account to %(account)s (the till\'s own account).',
                     till=till.display_name,
                     account=till.default_account_id.display_name or _('its cash account'))
        return False

    def _water_cash_sale_journal(self):
        self.ensure_one()
        return self.x_cash_sale_journal_id or self.env['account.journal'].sudo().search([
            ('type', '=', 'sale'), ('code', '=', 'CSAL'),
            ('company_id', '=', (self.company_id or self.env.company).id),
        ], limit=1)

    @api.model
    def _water_cash_partner_ids(self):
        """Every branch cash customer: valid wherever the buyer is."""
        return set(self.sudo().search([('x_cash_partner_id', '!=', False)])
                   .mapped('x_cash_partner_id').ids)


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def _water_location_check(self, picking_type, partner, latitude, longitude):
        # A cash customer stands for whoever walked in: there is no one site
        # to check the note against.
        if partner and picking_type and partner == picking_type.sudo().x_cash_partner_id:
            return None, None
        return super()._water_location_check(picking_type, partner, latitude, longitude)

    def _action_done(self, *args, **kwargs):
        res = super()._action_done(*args, **kwargs)
        # A cash note held for a dispatcher is invoiced when it is accepted.
        dispatches = self.env['ksw.water.dispatch'].sudo().search([
            ('picking_id', 'in', self.ids), ('x_is_cash', '=', True),
            ('x_cash_invoice_id', '=', False)])
        dispatches._water_invoice_cash()
        return res


class KswWaterDispatch(models.Model):
    _inherit = 'ksw.water.dispatch'

    x_is_cash = fields.Boolean(string='Cash Sale', tracking=True, copy=False)
    x_buyer_name = fields.Char(string='Buyer', tracking=True,
                               help='The person paying, when he gives a name.')
    x_buyer_phone = fields.Char(string='Buyer Phone')
    x_cash_amount = fields.Monetary(
        string='To Collect', compute='_compute_cash_amount',
        currency_field='x_currency_id',
        help='Price from the client\'s pricelist for this quantity, VAT included.')
    x_currency_id = fields.Many2one(related='company_id.currency_id')
    # The links are accounting's: a dispatcher has no rights on payments or
    # invoices, and a relational field he cannot read breaks his form (KSW
    # gotchas #5, #31). He reads the two plain mirrors below instead.
    x_cash_payment_id = fields.Many2one('account.payment', string='Cash Payment',
                                        readonly=True, copy=False,
                                        groups='account.group_account_invoice')
    x_cash_invoice_id = fields.Many2one('account.move', string='Cash Invoice',
                                        readonly=True, copy=False,
                                        groups='account.group_account_invoice')
    x_cash_received = fields.Monetary(string='Cash Received', readonly=True, copy=False,
                                      currency_field='x_currency_id')
    x_cash_invoice_ref = fields.Char(string='Invoice', readonly=True, copy=False)

    # Most cash sales are collected at the branch by the customer's own
    # tanker: nobody of ours drives, so the sale is delivered the moment it is
    # paid. 'ours' is the dispatched delivery, confirmed by our driver.
    x_collected_by = fields.Selection(
        [('customer', "Customer's own tanker"), ('ours', 'Our driver')],
        string='Delivered By', default='customer', tracking=True,
        help="Customer's own tanker: delivered the moment it is sold, at the "
             'branch. Our driver: dispatched, and delivered when he confirms it.')
    x_pickup_plate = fields.Char(string="Customer's Tanker Plate")
    x_pickup_driver = fields.Char(string="Customer's Driver")
    x_is_pickup = fields.Boolean(compute='_compute_is_pickup')

    x_cash_problem = fields.Char(
        compute='_compute_cash_problem',
        help='Why this cash sale cannot be priced or taken yet, in words the '
             'dispatcher can act on.')

    # The quantity fields are named, not only quantity_m3: that one is a plain
    # compute itself, and the form must re-price on every keystroke.
    @api.depends('x_is_cash', 'partner_id', 'product_id', 'quantity_m3',
                 'entered_qty', 'entered_uom', 'vehicle_id')
    def _compute_cash_amount(self):
        for dispatch in self:
            dispatch.x_cash_amount = dispatch._water_cash_total() if dispatch.x_is_cash else 0.0

    @api.depends('x_is_cash', 'x_collected_by')
    def _compute_is_pickup(self):
        for dispatch in self:
            dispatch.x_is_pickup = dispatch.x_is_cash and dispatch.x_collected_by == 'customer'

    @api.depends('x_is_cash', 'x_collected_by', 'picking_type_id', 'partner_id',
                 'product_id', 'entered_uom', 'vehicle_id')
    def _compute_cash_problem(self):
        Pricing = self.env['ksw.water.pricing']
        for dispatch in self:
            problem = False
            branch = dispatch.picking_type_id.sudo()
            if dispatch.x_is_cash and branch:
                if not branch.x_cash_partner_id:
                    problem = _('%s is not set up for cash sales yet. An administrator '
                                'presses Set Up Cash Sales in Water Delivery ▸ '
                                'Configuration ▸ Branches.', branch.display_name)
                elif not Pricing._products_for_partner(branch.x_cash_partner_id):
                    problem = _('%s has no cash prices yet. Add them for its cash '
                                'customer in Water Delivery ▸ Configuration ▸ Client '
                                'Rates.', branch.display_name)
                elif not branch.x_cash_journal_id:
                    problem = _('%s has no cash till set. Set it in Water Delivery ▸ '
                                'Configuration ▸ Branches.', branch.display_name)
                elif branch._water_till_problem():
                    problem = branch._water_till_problem()
                elif dispatch.product_id and not dispatch._water_income_account():
                    problem = dispatch._water_income_problem()
                elif dispatch.x_is_pickup and dispatch.entered_uom == 'trip':
                    problem = _("The customer's tanker is not ours, so its trip volume is "
                                "unknown: enter the quantity in the product's unit.")
                elif dispatch.entered_uom == 'trip' and not dispatch.vehicle_id:
                    problem = _('Choose the driver to price trips: his tanker decides '
                                'how many m³ a trip is.')
            dispatch.x_cash_problem = problem

    @api.model_create_multi
    def create(self, vals_list):
        # The client of a cash sale is the branch's cash customer, given, not
        # chosen: the form shows it read-only, and a read-only field never
        # reaches the server from the web client (KSW gotcha #145).
        Type = self.env['stock.picking.type'].sudo()
        for vals in vals_list:
            if vals.get('x_is_cash') and vals.get('picking_type_id'):
                cash = Type.browse(vals['picking_type_id']).x_cash_partner_id
                if cash:
                    vals['partner_id'] = cash.id
        return super().create(vals_list)

    def write(self, vals):
        res = super().write(vals)
        if {'x_is_cash', 'picking_type_id'} & set(vals):
            for dispatch in self.filtered('x_is_cash'):
                cash = dispatch.picking_type_id.sudo().x_cash_partner_id
                if cash and dispatch.partner_id != cash:
                    super(KswWaterDispatch, dispatch).write({'partner_id': cash.id})
        return res

    def _water_cash_total(self):
        self.ensure_one()
        qty = self.quantity_m3
        rate = self.env['ksw.water.pricing']._rate_for(self.partner_id, self.product_id, qty or 1.0)
        if not (rate and qty):
            return 0.0
        product = self.product_id._origin or self.product_id
        taxes = product.sudo().taxes_id.filtered(
            lambda t: t.company_id in (self.company_id, self.company_id.parent_ids))
        return taxes.compute_all(rate.fixed_price, self.company_id.currency_id, qty,
                                 product=product, partner=self.partner_id)['total_included']

    @api.depends('picking_type_id', 'x_is_cash')
    def _compute_allowed_partner_ids(self):
        super()._compute_allowed_partner_ids()
        for dispatch in self:
            cash = dispatch.picking_type_id.sudo().x_cash_partner_id
            if cash:
                dispatch.allowed_partner_ids |= cash

    @api.depends('picking_type_id', 'x_is_cash')
    def _compute_branch_in_dispatch_mode(self):
        super()._compute_branch_in_dispatch_mode()
        for dispatch in self.filtered('x_is_cash'):
            dispatch.branch_in_dispatch_mode = True

    @api.onchange('x_is_cash', 'picking_type_id')
    def _onchange_water_cash(self):
        cash = self.picking_type_id.sudo().x_cash_partner_id
        if self.x_is_cash:
            self.partner_id = cash
        elif self.partner_id and self.partner_id == cash:
            self.partner_id = False
        products = self.env['ksw.water.pricing']._products_for_partner(self.partner_id)
        if self.product_id._origin not in products:
            self.product_id = products[:1] if len(products) == 1 else False

    @api.onchange('x_collected_by', 'x_is_cash')
    def _onchange_water_pickup(self):
        if self.x_is_cash and self.x_collected_by == 'customer':
            self.entered_uom = 'product'
            self.driver_user_id = False

    # ------------------------------------------------------------------
    def action_dispatch(self):
        pickups = self.filtered('x_is_pickup')
        if pickups:
            pickups._water_sell_pickup()
        rest = self - pickups
        if not rest:
            return True
        return rest._water_dispatch_delivery()

    def _water_dispatch_delivery(self):
        cash = self.filtered('x_is_cash')
        cash._water_cash_checks()
        # The base action lets a cash dispatch past its issue-mode check;
        # the money is taken once everything else has been checked.
        res = super(KswWaterDispatch, self).action_dispatch()
        for dispatch in cash:
            dispatch._water_take_cash()
        return res

    def _water_income_account(self):
        """The account the cash invoice will credit: same order as
        sale.order.line._prepare_invoice_line (product override for the branch,
        the branch's Sales Account, then the product's own income account)."""
        self.ensure_one()
        product = self.product_id._origin or self.product_id
        return (self.env['ksw.water.revenue.account']._account_for_branch(
                    self.picking_type_id, product)
                or product.product_tmpl_id.sudo()._get_product_accounts().get('income'))

    def _water_income_problem(self):
        self.ensure_one()
        return _('%(branch)s has no Sales Account, so the sale has nowhere to be booked. '
                 'Set it on the branch: Water Delivery ▸ Configuration ▸ Branches ▸ '
                 'Sales Account.', branch=self.picking_type_id.sudo().display_name)

    def _water_cash_checks(self):
        for dispatch in self:
            if not dispatch._water_income_account():
                raise UserError(dispatch._water_income_problem())
            branch = dispatch.picking_type_id.sudo()
            if not branch.x_cash_journal_id:
                raise UserError(_(
                    '%s has no cash till set. Set it in Water Delivery ▸ '
                    'Configuration ▸ Branches.', branch.display_name))
            if branch._water_till_problem():
                raise UserError(branch._water_till_problem())
            if not dispatch._water_cash_total():
                raise UserError(_(
                    'There is no cash price for %(product)s with %(client)s. Add '
                    'it in Client Rates.', product=dispatch.product_id.display_name,
                    client=dispatch.partner_id.display_name))

    def _water_sell_pickup(self):
        """Customer collects with his own tanker: paid and delivered at once."""
        self._check_branch_authority()
        for dispatch in self:
            if dispatch.state != 'draft':
                raise UserError(_('Only a draft can be sold.'))
            if dispatch.entered_uom != 'product':
                raise UserError(_("The customer's tanker is not ours, so its trip volume "
                                  "is unknown: enter the quantity in the product's unit."))
            if dispatch.entered_qty <= 0:
                raise UserError(_('The quantity must be greater than zero.'))
        self._water_cash_checks()
        now = fields.Datetime.now()
        for dispatch in self:
            values = {'state': 'delivered', 'dispatcher_id': self.env.uid,
                      'dispatched_on': now, 'delivered_on': now,
                      'driver_user_id': False, 'vehicle_id': False}
            if dispatch.name in (False, 'New', _('New')):
                values['name'] = self.env['ir.sequence'].sudo().next_by_code(
                    'ksw.water.dispatch') or _('New')
            dispatch.sudo().write(values)
            dispatch._water_take_cash()
            dispatch._water_issue_pickup_note()
            dispatch._water_invoice_cash()

    def _water_issue_pickup_note(self):
        """The delivery note of a pickup: the same native chain as a driver's
        note (sale order at the cash price, its delivery re-homed onto the
        branch, which stamps it a water note and takes the branch's stock),
        validated at once. No tanker of ours, no signature pad, no position:
        the buyer is standing at the desk."""
        self.ensure_one()
        dispatch = self.sudo()
        branch = dispatch.picking_type_id
        qty = dispatch.entered_qty
        rate = self.env['ksw.water.pricing']._rate_for(dispatch.partner_id, dispatch.product_id, qty)
        order = self.env['sale.order'].sudo().create({
            'partner_id': dispatch.partner_id.id,
            'company_id': branch.company_id.id or dispatch.company_id.id,
            'warehouse_id': branch.warehouse_id.id,
            'pricelist_id': rate.pricelist_id.id,
            'order_line': [(0, 0, {
                'product_id': dispatch.product_id.id,
                'product_uom_qty': qty,
                'price_unit': rate.fixed_price,
                'name': dispatch.product_id.get_product_multiline_description_sale(),
            })],
        })
        order.action_confirm()
        picking = order.picking_ids
        if len(picking) != 1:
            raise UserError(_(
                'Expected exactly one delivery for this order, got %s. Ask a '
                'supervisor to check the warehouse configuration.', len(picking)))
        picking.write({'picking_type_id': branch.id})
        collector = ' / '.join(filter(None, [dispatch.x_pickup_driver, dispatch.x_pickup_plate]))
        picking.write({
            'x_entered_qty': qty, 'x_entered_uom': 'product',
            'x_dispatch_id': dispatch.id,
            'x_signed_by': dispatch.x_pickup_driver or dispatch.x_buyer_name or False,
        })
        picking.move_ids.write({'quantity': qty, 'picked': True})
        picking.with_context(skip_backorder=True).button_validate()
        if collector:
            picking.message_post(body=Markup('<strong>%(title)s</strong> %(who)s') % {
                'title': _("Collected by the customer's tanker:"), 'who': collector})
        dispatch.picking_id = picking
        picking._water_stamp_analytic()

    def _water_take_cash(self):
        self.ensure_one()
        branch = self.picking_type_id.sudo()
        amount = self._water_cash_total()
        payment = self.env['account.payment'].sudo().create({
            'payment_type': 'inbound',
            'partner_type': 'customer',
            'partner_id': self.partner_id.commercial_partner_id.id,
            'amount': amount,
            'journal_id': branch.x_cash_journal_id.id,
            'date': fields.Date.context_today(self),
            'memo': ' / '.join(filter(None, [self.name, self.x_buyer_name, self.x_buyer_phone])),
            'company_id': self.company_id.id,
        })
        payment.action_post()
        self.sudo().write({'x_cash_payment_id': payment.id, 'x_cash_received': amount})
        self.sudo().message_post(body=Markup(
            '<strong>%(title)s</strong> %(amount)s %(cur)s → %(till)s') % {
            'title': _('Cash received at the desk:'), 'amount': '%.2f' % amount,
            'cur': self.company_id.currency_id.name, 'till': branch.x_cash_journal_id.name})

    def _water_refund_cash(self):
        for dispatch in self.sudo().filtered('x_cash_payment_id'):
            payment = dispatch.x_cash_payment_id.sudo()
            if payment.state not in ('canceled', 'cancel'):
                payment.action_draft()
                payment.action_cancel()
            dispatch.sudo().message_post(body=Markup('<strong>%(title)s</strong> %(amount)s') % {
                'title': _('Cash handed back:'), 'amount': '%.2f' % payment.amount})
            dispatch.sudo().write({'x_cash_payment_id': False, 'x_cash_received': 0.0})

    def action_draft(self):
        res = super().action_draft()
        self._water_refund_cash()
        return res

    def action_cancel(self):
        res = super().action_cancel()
        self._water_refund_cash()
        return res

    def _water_on_capture_issued(self, capture):
        res = super()._water_on_capture_issued(capture)
        self.filtered('x_is_cash')._water_invoice_cash()
        return res

    def _water_invoice_cash(self):
        """Invoice a delivered cash sale at once and match it to the cash."""
        for dispatch in self.sudo().filtered(
                lambda d: d.x_is_cash and not d.x_cash_invoice_id and d.picking_id):
            picking = dispatch.picking_id
            order = picking.sale_id
            if picking.state != 'done' or not order:
                continue   # held for a dispatcher: invoiced when accepted
            journal = dispatch.picking_type_id._water_cash_sale_journal()
            if journal:
                order.journal_id = journal
            invoice = order._create_invoices()
            buyer = ' / '.join(filter(None, [dispatch.x_buyer_name, dispatch.x_buyer_phone]))
            invoice.write({
                'ref': ' / '.join(filter(None, [dispatch.name, buyer])),
                'invoice_date': fields.Date.context_today(dispatch),
            })
            dispatch.write({'x_cash_invoice_id': invoice.id,
                            'x_cash_invoice_ref': _('Draft invoice')})
            try:
                with self.env.cr.savepoint():
                    invoice.action_post()
            except UserError as error:
                # ZATCA refuses to post until the company is onboarded; the
                # cash is already in the till either way. Accounting posts the
                # draft later and _post() settles it then.
                dispatch.message_post(body=Markup(
                    '<strong>%(title)s</strong><br/>%(error)s') % {
                    'title': _('Cash invoice left in draft, accounting must post it:'),
                    'error': str(error)})
                continue
            dispatch.write({'x_cash_invoice_ref': invoice.name})
            dispatch._water_settle_cash()

    def _water_settle_cash(self):
        """Match the posted cash invoice with the cash taken at the desk."""
        for dispatch in self.sudo():
            invoice, payment = dispatch.x_cash_invoice_id, dispatch.x_cash_payment_id
            if not (invoice and payment) or invoice.state != 'posted':
                continue
            if dispatch.x_cash_invoice_ref != invoice.name:
                dispatch.x_cash_invoice_ref = invoice.name
            lines = (invoice.line_ids | payment.move_id.line_ids).filtered(
                lambda l: l.account_id.account_type == 'asset_receivable'
                and not l.reconciled)
            if len(lines.move_id) > 1:
                lines.reconcile()


class AccountMove(models.Model):
    _inherit = 'account.move'

    def _post(self, soft=True):
        posted = super()._post(soft=soft)
        invoices = posted.filtered(lambda m: m.move_type == 'out_invoice')
        if invoices:
            self.env['ksw.water.dispatch'].sudo().search(
                [('x_cash_invoice_id', 'in', invoices.ids)])._water_settle_cash()
        return posted
