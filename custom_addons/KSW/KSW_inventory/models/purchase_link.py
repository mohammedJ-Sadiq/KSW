"""Connect the BAS purchases (vendor bills) to the stock items they bought.

BAS records a stock purchase (``006``) as an account and a line of free text:
Dr 1206010003 "عدد10كفر جي تي315/80 التميمي ف90859393" / Cr supplier. No item
code, no quantity column. KSW_bas_gl_import turned each into a vendor bill
line the same way: no product, quantity 1, the whole amount as unit price.
So a product had no purchase history even though its purchases are in the
books.

Matching the text automatically is not good enough to trust: brands are
spelt differently on bills and items («ماجنا» / «ميجانا»), sizes repeat across
brands, and one line can carry two items. So it is a **review**: every
stock-purchase bill line gets a row here with a *suggestion*, and a person
confirms the item and quantity (or splits the line, or marks it as not a
stock item). Nothing is linked unconfirmed.

What a confirmation writes, and what it deliberately does not:

* ``ksw.bas.purchase.part`` rows (item, quantity, amount): the purchase
  history the product's **Vendor Bills** button lists. A split line has one
  part per item.
* A single-item line also gets product, quantity and unit price **on the bill
  line itself** (quantity × unit price = the same subtotal, so no balance,
  account or tax changes; checked on a posted bill). A split line cannot be
  split on a posted, paid bill without unposting it, so the bill keeps it as
  one line and the parts carry the detail.
* **No stock moves.** On-hand was loaded from BAS's register after these
  purchases, so receiving them again would count them twice.
"""
import difflib
import re

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_QTY_RE = re.compile(r'عدد\s*(\d+)')
_SIZE_RE = re.compile(r'\d{2,3}\s*/\s*\d{2,3}')
_INVOICE_RE = re.compile(r'ف\s*[\d\-/]+')
_NOISE = {'كفر', 'كفرات', 'عدد', 'للمستودع', 'جديد', 'المستودع', 'شركه', 'مؤسسه', 'r22.5', 'r22'}


def _norm(text):
    text = (text or '').lower()
    for a, b in (('ة', 'ه'), ('أ', 'ا'), ('إ', 'ا'), ('آ', 'ا'), ('ى', 'ي')):
        text = text.replace(a, b)
    return re.sub(r'\s+', ' ', text).strip()


def _sizes(text):
    """Tyre sizes, either way round (BAS writes 315/80 and 80/315)."""
    out = set()
    for s in _SIZE_RE.findall(text):
        a, b = [x.strip() for x in s.split('/')]
        out.add('/'.join(sorted((a, b))))
    return out


def _words(text):
    """Name words. BAS glues words to numbers («ماجنا315/80», «عدد10كفر»)."""
    text = _SIZE_RE.sub(' ', _INVOICE_RE.sub(' ', _QTY_RE.sub(' ', text)))
    text = re.sub(r'(?<=\d)(?=[^\d\s])|(?<=[^\d\s])(?=\d)', ' ', text)
    return {w for w in re.split(r'[^\w]+', text) if len(w) >= 2 and not w.isdigit()} - _NOISE


def _skeleton(word):
    """Consonants only: «ماجنا» and «ميجانا» are both «مجن»."""
    if word.startswith('ال') and len(word) > 4:
        word = word[2:]
    return re.sub('[اويىه]', '', word)


def _several_items(text):
    return '+' in text or len(_SIZE_RE.findall(text)) > 1 or len(_QTY_RE.findall(text)) > 1


def _pattern(text):
    """The line without its quantity and invoice number: repeats across bills."""
    text = _INVOICE_RE.sub(' ', _QTY_RE.sub(' ', _norm(text)))
    return re.sub(r'[\s\d]+$', '', re.sub(r'\s+', ' ', text)).strip()


class KswBasPurchaseReview(models.Model):
    _name = 'ksw.bas.purchase.review'
    _description = 'BAS Purchase to Link'
    _order = 'date desc, id desc'
    _rec_name = 'text'

    line_id = fields.Many2one('account.move.line', string='Bill Line', required=True,
                              ondelete='cascade', index=True, readonly=True)
    move_id = fields.Many2one(related='line_id.move_id', string='Vendor Bill', store=True)
    # As text: an inventory user may not read account.move at all.
    bill_ref = fields.Char(related='line_id.move_id.name', string='Bill', store=True)
    date = fields.Date(related='line_id.date', store=True)
    partner_id = fields.Many2one('res.partner', string='Vendor', compute='_compute_partner', store=True)
    text = fields.Char(related='line_id.name', string='Description', store=True)
    account_id = fields.Many2one(related='line_id.account_id', store=True)
    amount = fields.Monetary(string='Amount', compute='_compute_amount', store=True)
    currency_id = fields.Many2one(related='line_id.currency_id')
    pattern = fields.Char(index=True, readonly=True,
                          help='The description without quantity and invoice number.')

    state = fields.Selection([
        ('review', 'To Review'),
        ('linked', 'Linked'),
        ('not_stock', 'Not a Stock Item'),
    ], default='review', required=True, readonly=True, index=True)
    suggestion = fields.Char(readonly=True, help='Why the item was suggested.')

    product_id = fields.Many2one(
        'product.product', string='Item', domain="[('id', 'in', allowed_product_ids)]",
        help='The stock item bought. For a line holding several items, use Split.')
    quantity = fields.Float(string='Quantity', digits='Product Unit')
    unit_price = fields.Monetary(string='Unit Price', compute='_compute_unit_price')
    allowed_product_ids = fields.Many2many('product.product', compute='_compute_allowed_products')
    part_ids = fields.One2many('ksw.bas.purchase.part', 'review_id', string='Items')
    part_count = fields.Integer(compute='_compute_unit_price')

    _line_uniq = models.Constraint('UNIQUE (line_id)', 'This bill line is already listed.')

    @api.depends('line_id.partner_id', 'line_id.move_id.partner_id')
    def _compute_partner(self):
        for rec in self:
            move = rec.line_id.move_id
            rec.partner_id = rec.line_id.partner_id or move.partner_id or \
                self._vendor_of_move(move)

    @api.model
    def _vendor_of_move(self, move):
        """The vendor of a purchase booked as a journal entry with no partner.

        BAS keeps one 2101 account per supplier, so the supplier is whoever
        the other bills on the same payable account are made out to.
        """
        payable = move.sudo().line_ids.filtered(
            lambda l: l.account_id.account_type == 'liability_payable').account_id
        if len(payable) != 1:
            return self.env['res.partner']
        if payable.x_bas_code:
            vendor = self.env['res.partner'].sudo().search(
                [('x_bas_code', '=', payable.x_bas_code)], limit=1)
            if vendor:
                return vendor
        data = self.env['account.move.line'].sudo()._read_group(
            [('account_id', '=', payable.id), ('partner_id', '!=', False),
             ('move_id.move_type', 'in', ('in_invoice', 'in_refund'))],
            ['partner_id'], ['__count'], order='__count desc', limit=1)
        return data[0][0] if data else self.env['res.partner']

    @api.depends('line_id.price_subtotal', 'line_id.balance', 'line_id.move_id.move_type')
    def _compute_amount(self):
        # A purchase imported as a journal entry has no quantity × price: its
        # amount is the debit on the stock account.
        for rec in self:
            line = rec.line_id
            rec.amount = line.price_subtotal if line.move_id.is_invoice() else abs(line.balance)

    @api.depends('amount', 'quantity', 'part_ids')
    def _compute_unit_price(self):
        for rec in self:
            rec.unit_price = rec.amount / rec.quantity if rec.quantity else 0.0
            rec.part_count = len(rec.part_ids)

    @api.depends('account_id')
    def _compute_allowed_products(self):
        for rec in self:
            rec.allowed_product_ids = rec._candidate_products()

    def _candidate_products(self):
        """Storable items whose category stocks into this line's account."""
        self.ensure_one()
        categories = self.env['product.category'].search([
            ('property_stock_valuation_account_id', '=', self.account_id.id)])
        return self.env['product.product'].search([
            ('categ_id', 'child_of', categories.ids), ('is_storable', '=', True)])

    # ------------------------------------------------------------------
    # Collecting and suggesting
    # ------------------------------------------------------------------
    @api.model
    def _stock_accounts(self):
        categories = self.env['product.category'].search([
            ('property_stock_valuation_account_id', '!=', False)])
        products = self.env['product.product'].search_count(
            [('categ_id', 'in', categories.ids), ('is_storable', '=', True)])
        return categories.mapped('property_stock_valuation_account_id') if products else \
            self.env['account.account']

    @api.model
    def action_collect(self):
        """List every stock-purchase bill line not listed yet, with a suggestion.

        Run as sudo after the rights check: the Inventory Administrator who
        does this review has no access to journal items of his own.
        """
        self._check_rights()
        self = self.sudo()
        accounts = self._stock_accounts()
        listed = self.search([]).mapped('line_id')
        lines = self.env['account.move.line'].search([
            ('move_id.move_type', 'in', ('in_invoice', 'in_refund')),
            ('move_id.state', '=', 'posted'),
            ('display_type', '=', 'product'),
            ('account_id', 'in', accounts.ids),
            ('id', 'not in', listed.ids),
        ])
        reviews = self.create([{'line_id': line.id, 'pattern': _pattern(line.name)}
                               for line in lines])
        reviews._suggest()
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'type': 'success', 'next': {'type': 'ir.actions.act_window_close'},
                       'message': _('%(n)s new purchase lines to review.', n=len(reviews))},
        }

    def _suggest(self):
        """Prefill item and quantity. Never confirms anything."""
        learned = {}
        for done in self.search([('state', '=', 'linked'), ('pattern', '!=', False)]):
            if len(done.part_ids) == 1:
                learned.setdefault(done.pattern, done.part_ids.product_id)
        for rec in self.filtered(lambda r: r.state == 'review' and not r.product_id):
            text = _norm(rec.text)
            qtys = _QTY_RE.findall(text)
            several = _several_items(text)
            vals = {'quantity': float(qtys[0]) if len(qtys) == 1 and not several else 0.0}
            if several:
                vals['suggestion'] = _('Several items on one line: split it')
            elif rec.pattern in learned and learned[rec.pattern] in rec._candidate_products():
                vals.update(product_id=learned[rec.pattern].id,
                            suggestion=_('Same description as a line already linked'))
            else:
                product, why = rec._match(text)
                if product:
                    vals.update(product_id=product.id, suggestion=why)
            rec.write(vals)

    def _match(self, text):
        """(product, reason) when exactly one candidate fits, else (False, False)."""
        sizes, words = _sizes(text), _words(text)
        scored = []
        for product in self._candidate_products():
            name = _norm(product.name)
            psizes = _sizes(name)
            if sizes and psizes and not (sizes & psizes):
                continue
            pwords = _words(name)
            best = 0.0
            for w in words:
                for pw in pwords:
                    if min(len(w), len(pw)) <= 2:
                        # «جي», «تي»: too short to be near, only equal.
                        ratio = 1.0 if w == pw else 0.0
                    elif len(_skeleton(w)) >= 3 and _skeleton(w) == _skeleton(pw):
                        ratio = 0.95
                    else:
                        ratio = difflib.SequenceMatcher(None, w, pw).ratio()
                    best = max(best, ratio)
            if best >= 0.8 and (not sizes or psizes):
                scored.append((best, product))
        if not scored:
            return False, False
        scored.sort(key=lambda s: -s[0])
        if len(scored) > 1 and scored[1][0] >= scored[0][0] - 0.05:
            return False, False
        return scored[0][1], _('Name and size match (%(pct)s%%)', pct=int(scored[0][0] * 100))

    # ------------------------------------------------------------------
    # Decisions
    # ------------------------------------------------------------------
    def _check_rights(self):
        if not self.env.su and not self.env.user.has_group('stock.group_stock_manager'):
            raise UserError(_('Only an Inventory Administrator can link purchases.'))

    def action_confirm(self):
        self._check_rights()
        for rec in self:
            if rec.state != 'review':
                continue
            if not rec.part_ids:
                if not rec.product_id or rec.quantity <= 0:
                    raise UserError(_('Choose the item and the quantity for "%(text)s", or split it.',
                                      text=rec.text))
                rec.part_ids = [fields.Command.create({
                    'product_id': rec.product_id.id, 'quantity': rec.quantity,
                    'amount': rec.amount})]
            parts = rec.part_ids
            if any(p.quantity <= 0 or not p.product_id for p in parts):
                raise UserError(_('Every item needs a product and a quantity.'))
            if abs(sum(parts.mapped('amount')) - rec.amount) > 0.01:
                raise UserError(_(
                    'The items add up to %(parts)s but the bill line is %(line)s.',
                    parts=round(sum(parts.mapped('amount')), 2), line=rec.amount))
            if len(parts) == 1 and rec.line_id.move_id.is_invoice():
                # Same subtotal, so the posted bill's balance does not move.
                # (A journal entry's line has no quantity × price to keep.)
                line = rec.line_id.sudo()
                line.write({
                    'product_id': parts.product_id.id,
                    'product_uom_id': parts.product_id.uom_id.id,
                    'quantity': parts.quantity,
                    'price_unit': line.price_unit * line.quantity / parts.quantity,
                })
            rec.state = 'linked'
        return True

    def action_not_stock(self):
        self._check_rights()
        self.filtered(lambda r: r.state == 'review').write({'state': 'not_stock'})
        return True

    def action_reset(self):
        self._check_rights()
        for rec in self.filtered(lambda r: r.state != 'review'):
            line = rec.line_id.sudo()
            if rec.state == 'linked' and len(rec.part_ids) == 1 and line.product_id:
                line.write({'product_id': False, 'price_unit': line.price_subtotal, 'quantity': 1.0})
            rec.part_ids.unlink()
            rec.state = 'review'
        return True

    def action_split(self):
        self.ensure_one()
        if not self.part_ids and self.product_id and self.quantity:
            self.part_ids = [fields.Command.create({
                'product_id': self.product_id.id, 'quantity': self.quantity,
                'amount': self.amount})]
        return {
            'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
            'view_mode': 'form', 'target': 'new', 'name': _('Split into Items'),
        }


class KswBasPurchasePart(models.Model):
    _name = 'ksw.bas.purchase.part'
    _description = 'Purchase History Item'
    _order = 'date desc, id desc'

    review_id = fields.Many2one('ksw.bas.purchase.review', required=True, ondelete='cascade',
                                index=True)
    move_id = fields.Many2one(related='review_id.move_id', store=True, string='Vendor Bill')
    bill_ref = fields.Char(related='review_id.bill_ref', store=True, string='Bill')
    date = fields.Date(related='review_id.date', store=True)
    partner_id = fields.Many2one(related='review_id.partner_id', store=True, string='Vendor')
    text = fields.Char(related='review_id.text', string='Bill Description')
    state = fields.Selection(related='review_id.state', store=True)
    product_id = fields.Many2one('product.product', string='Item', required=True, index=True,
                                 domain="[('is_storable', '=', True)]")
    product_tmpl_id = fields.Many2one(related='product_id.product_tmpl_id', store=True, index=True)
    quantity = fields.Float(string='Quantity', digits='Product Unit', required=True)
    amount = fields.Monetary(string='Amount', required=True)
    currency_id = fields.Many2one(related='review_id.currency_id')
    unit_price = fields.Monetary(string='Unit Price', compute='_compute_unit_price')

    @api.depends('amount', 'quantity')
    def _compute_unit_price(self):
        for rec in self:
            rec.unit_price = rec.amount / rec.quantity if rec.quantity else 0.0

    def action_open_bill(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'account.move',
                'res_id': self.move_id.id, 'view_mode': 'form', 'target': 'current'}


class KswProductPurchaseHistory(models.Model):
    """One purchase history per item, the same before and after the move to Odoo.

    Three sources, one list:

    * ``bas``: BAS purchases linked through the review (BAS has no orders:
      the bill *is* the purchase, so these rows belong to both buttons);
    * ``order``: Odoo purchase-order lines (confirmed);
    * ``bill``: Odoo vendor-bill lines carrying a product, **except** the
      lines the review covers (a confirmed single-item line has its product
      written on the bill line, and would otherwise count twice).

    **Purchased** reads ``bas`` + ``order``; **Vendor Bills** reads ``bas`` +
    ``bill``. So an item's history runs on unchanged when buying moves from
    BAS to Odoo, and in Odoo an ordered-and-billed item appears once per button.
    """
    _name = 'ksw.product.purchase.history'
    _description = 'Item Purchase History'
    _auto = False
    _order = 'date desc, id desc'
    # What the query reads, so pending writes are flushed before it runs.
    _depends = {
        'ksw.bas.purchase.part': ['review_id', 'date', 'partner_id', 'bill_ref', 'move_id',
                                  'product_id', 'product_tmpl_id', 'quantity', 'amount'],
        'ksw.bas.purchase.review': ['state', 'line_id', 'text'],
        'purchase.order': ['state', 'date_approve', 'partner_id', 'name', 'currency_id'],
        'purchase.order.line': ['order_id', 'product_id', 'product_uom_qty', 'price_subtotal',
                                'display_type', 'name'],
        'account.move': ['move_type', 'state', 'invoice_date', 'date', 'partner_id', 'name',
                         'currency_id'],
        'account.move.line': ['move_id', 'product_id', 'quantity', 'price_subtotal', 'price_unit',
                              'display_type', 'name'],
    }

    kind = fields.Selection([
        ('bas', 'BAS Purchase'),
        ('order', 'Purchase Order'),
        ('bill', 'Vendor Bill'),
    ], string='Source', readonly=True)
    date = fields.Date(readonly=True)
    partner_id = fields.Many2one('res.partner', string='Vendor', readonly=True)
    document = fields.Char(string='Document', readonly=True)
    move_id = fields.Many2one('account.move', string='Vendor Bill', readonly=True)
    order_id = fields.Many2one('purchase.order', string='Purchase Order', readonly=True)
    product_id = fields.Many2one('product.product', string='Item', readonly=True)
    product_tmpl_id = fields.Many2one('product.template', readonly=True)
    quantity = fields.Float(string='Quantity', digits='Product Unit', readonly=True)
    amount = fields.Monetary(string='Amount', readonly=True)
    unit_price = fields.Monetary(string='Unit Price', readonly=True, aggregator=False)
    currency_id = fields.Many2one('res.currency', readonly=True)
    description = fields.Char(readonly=True)

    @property
    def _table_query(self):
        return """
            SELECT p.id * 3 AS id, 'bas' AS kind, p.date, p.partner_id,
                   p.bill_ref AS document, p.move_id, NULL::int AS order_id,
                   p.product_id, p.product_tmpl_id, p.quantity, p.amount,
                   CASE WHEN p.quantity <> 0 THEN p.amount / p.quantity END AS unit_price,
                   m.currency_id, r.text AS description
              FROM ksw_bas_purchase_part p
              JOIN ksw_bas_purchase_review r ON r.id = p.review_id
              JOIN account_move m ON m.id = p.move_id
             WHERE r.state = 'linked'
            UNION ALL
            SELECT l.id * 3 + 1, 'order', o.date_approve::date, o.partner_id,
                   o.name, NULL, o.id, l.product_id, pp.product_tmpl_id,
                   l.product_uom_qty, l.price_subtotal,
                   CASE WHEN l.product_uom_qty <> 0 THEN l.price_subtotal / l.product_uom_qty END,
                   o.currency_id, l.name
              FROM purchase_order_line l
              JOIN purchase_order o ON o.id = l.order_id
              JOIN product_product pp ON pp.id = l.product_id
             WHERE o.state = 'purchase' AND l.display_type IS NULL
            UNION ALL
            SELECT l.id * 3 + 2, 'bill', COALESCE(m.invoice_date, m.date), m.partner_id,
                   m.name, m.id, NULL, l.product_id, pp.product_tmpl_id,
                   CASE WHEN m.move_type = 'in_refund' THEN -l.quantity ELSE l.quantity END,
                   CASE WHEN m.move_type = 'in_refund' THEN -l.price_subtotal ELSE l.price_subtotal END,
                   l.price_unit, m.currency_id, l.name
              FROM account_move_line l
              JOIN account_move m ON m.id = l.move_id
              JOIN product_product pp ON pp.id = l.product_id
             WHERE m.move_type IN ('in_invoice', 'in_refund') AND m.state = 'posted'
               AND l.display_type = 'product'
               AND NOT EXISTS (SELECT 1 FROM ksw_bas_purchase_review r WHERE r.line_id = l.id)
        """

    def action_open_document(self):
        self.ensure_one()
        model, res_id = ('purchase.order', self.order_id.id) if self.order_id else \
            ('account.move', self.move_id.id)
        return {'type': 'ir.actions.act_window', 'res_model': model, 'res_id': res_id,
                'view_mode': 'form', 'target': 'current'}


_PURCHASED = ('bas', 'order')
_BILLED = ('bas', 'bill')


def _history_action(env, field, ids, kinds, title, last_year=False):
    action = env['ir.actions.act_window']._for_xml_id('KSW_inventory.action_ksw_product_purchase_history')
    action['domain'] = [(field, 'in', ids), ('kind', 'in', kinds)]
    action['display_name'] = title
    if last_year:
        # The same window as core's Purchased figure; one click removes it.
        action['context'] = {'search_default_last_year': 1}
    return action


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    x_vendor_bill_count = fields.Integer(compute='_compute_vendor_bill_count')

    def _compute_vendor_bill_count(self):
        data = dict(self.env['ksw.product.purchase.history']._read_group(
            [('product_tmpl_id', 'in', self.ids), ('kind', 'in', _BILLED)],
            ['product_tmpl_id'], ['__count']))
        for tmpl in self:
            tmpl.x_vendor_bill_count = data.get(tmpl, 0)

    def action_view_vendor_bill_history(self):
        return _history_action(self.env, 'product_tmpl_id', self.ids, _BILLED,
                               _('Vendor Bills for %s', self.display_name))

    def action_view_po(self):
        super().action_view_po()
        return _history_action(self.env, 'product_tmpl_id', self.ids, _PURCHASED,
                               _('Purchase History for %s', self.display_name), last_year=True)


class ProductProduct(models.Model):
    _inherit = 'product.product'

    # The variant form is built from the template form, so it needs the
    # field the button reads, counted for this variant only.
    x_vendor_bill_count = fields.Integer(compute='_compute_vendor_bill_count')

    def _compute_vendor_bill_count(self):
        data = dict(self.env['ksw.product.purchase.history']._read_group(
            [('product_id', 'in', self.ids), ('kind', 'in', _BILLED)],
            ['product_id'], ['__count']))
        for product in self:
            product.x_vendor_bill_count = data.get(product, 0)

    def _compute_purchased_product_qty(self):
        """Core's figure (Odoo orders, last 365 days) plus the BAS purchases."""
        super()._compute_purchased_product_qty()
        date_from = fields.Date.context_today(self) - relativedelta(years=1)
        bas = dict(self.env['ksw.product.purchase.history'].sudo()._read_group(
            [('product_id', 'in', self.ids), ('kind', '=', 'bas'), ('date', '>=', date_from)],
            ['product_id'], ['quantity:sum']))
        for product in self:
            if product.id and product in bas:
                product.purchased_product_qty = product.uom_id.round(
                    product.purchased_product_qty + bas[product])

    def action_view_vendor_bill_history(self):
        return _history_action(self.env, 'product_id', self.ids, _BILLED,
                               _('Vendor Bills for %s', self.display_name))

    def action_view_po(self):
        super().action_view_po()
        return _history_action(self.env, 'product_id', self.ids, _PURCHASED,
                               _('Purchase History for %s', self.display_name), last_year=True)
