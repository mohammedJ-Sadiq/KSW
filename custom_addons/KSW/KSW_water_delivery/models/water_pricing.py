import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# BAS holds no pricelist table: ITM10.ISPRICE1..4 are zero on every item. The
# rate a client actually pays lives on each delivery-note line (str10.PRICE),
# and it is stable -- 322 of 389 (client, item) pairs were charged exactly one
# price over six months. So the client pricelists are seeded from the newest
# price per pair rather than from a table that does not exist.
_BAS_RATE_SQL = """
    SELECT FCODE, ICODE, PRICE, IUNIT, CODE2, FDATE FROM (
        SELECT s.FCODE, s.ICODE, s.PRICE, s.IUNIT, s.CODE2, s.FDATE,
               ROW_NUMBER() OVER (PARTITION BY s.FCODE, s.ICODE
                                  ORDER BY s.FDATE DESC) AS rn
          FROM str10 s
         WHERE s.FTYPE = '600' AND s.PRICE > 0
           AND s.FCODE IS NOT NULL AND s.FCODE <> ''
           -- 1203* is the trade-customer ledger. 1201* is internal cash and
           -- treasury (حساب الصندوق) -- those are the CASH delivery notes, and
           -- the till is not a client.
           AND s.FCODE LIKE '1203%%'
           AND s.FDATE >= DATEADD(month, -%(months)s, GETDATE())
    ) t WHERE rn = 1
"""

# Which branch delivers to which client. A separate question from the rate:
# the price is agreed with the client, the coverage is where the trucks go, and
# 93 of the 319 clients are served by more than one branch.
_BAS_COVERAGE_SQL = """
    SELECT s.FCODE, s.CODE2, MAX(s.FDATE) AS last_delivery
      FROM str10 s
     WHERE s.FTYPE = '600'
       AND s.FCODE IS NOT NULL AND s.FCODE <> ''
       AND s.FCODE LIKE '1203%%'
       AND s.CODE2 IS NOT NULL AND s.CODE2 <> ''
       AND s.FDATE >= DATEADD(month, -%(months)s, GETDATE())
     GROUP BY s.FCODE, s.CODE2
"""

# A client rate is a fixed price for one product. Category, global and formula
# rules price things too, but they are not an agreement about what this client
# buys from the tanker, so they never put a client on a driver's list.
_RATE_RULE_DOMAIN = [
    ('compute_price', '=', 'fixed'),
    ('applied_on', 'in', ('0_product_variant', '1_product')),
]


class KswWaterPricing(models.AbstractModel):
    """Water prices come from the client's pricelist and nowhere else.

    Until Oct 2026 the module kept its own (client, product, price) register,
    `ksw.water.rate`, next to Odoo's pricelists: the Sales app priced from one,
    the delivery notes from the other, and nothing compared them. Now the
    client's own pricelist (the one assigned on the contact) answers all three
    questions the register used to:

    * who a driver may deliver to: clients whose assigned pricelist holds a
      fixed-price rule for a product;
    * which products: the products of those rules;
    * the price: the rule Odoo's own pricing picks for that quantity, and
      nothing at all when that is not a fixed rule -- a note is never priced
      at the product's list price by falling through.

    Only a pricelist ASSIGNED on the contact counts. Odoo hands every other
    partner the company default pricelist; a fixed rule there would otherwise
    put all 800+ customers on every driver's list.
    """
    _name = 'ksw.water.pricing'
    _description = 'Water Delivery Pricing'

    @api.model
    def _client_pricelist(self, partner):
        # `_origin`: read from an unsaved form record (an onchange), a
        # relational value comes back NewId-wrapped, and searching on a NewId
        # matches nothing and raises nothing (KSW gotcha #149).
        partner = partner._origin if partner else partner
        if not partner:
            return self.env['product.pricelist']
        return partner.commercial_partner_id.sudo().with_company(
            self.env.company).specific_property_product_pricelist

    @api.model
    def _rate_rules(self, pricelists, date=None):
        now = date or fields.Datetime.now()
        return self.env['product.pricelist.item'].sudo().search([
            ('pricelist_id', 'in', pricelists.ids),
            ('pricelist_id.active', '=', True),
            *_RATE_RULE_DOMAIN,
            '|', ('date_start', '=', False), ('date_start', '<=', now),
            '|', ('date_end', '=', False), ('date_end', '>=', now),
        ])

    @api.model
    def _rule_products(self, rules):
        products = rules.product_id | rules.filtered(
            lambda r: r.applied_on == '1_product').product_tmpl_id.product_variant_ids
        return products.filtered(lambda p: p.active and p.sale_ok)

    @api.model
    def _rated_partner_ids(self):
        """Clients the drivers may deliver to: exactly those we can price."""
        company = self.env.company
        Pricelist = self.env['product.pricelist'].sudo()
        rules = self._rate_rules(Pricelist.search(
            [('company_id', 'in', (company.id, False))]))
        pricelists = rules.filtered(
            lambda r: self._rule_products(r)).pricelist_id
        if not pricelists:
            return []
        return self.env['res.partner'].sudo().with_company(company).search([
            ('specific_property_product_pricelist', 'in', pricelists.ids),
            ('parent_id', '=', False),
        ]).ids

    @api.model
    def _products_for_partner(self, partner):
        pricelist = self._client_pricelist(partner)
        if not pricelist:
            return self.env['product.product']
        return self._rule_products(self._rate_rules(pricelist))

    @api.model
    def _rate_for(self, partner, product, quantity=1.0):
        """The client's fixed-price rule for this product and quantity.

        Odoo's own rule selection (`_get_product_rule`), so a quantity break
        or a dated price change on the pricelist applies here exactly as on a
        sales order. Empty when the rule Odoo would use is not a fixed price
        -- including when no rule matches and the price would silently fall
        through to the product's list price.
        """
        Item = self.env['product.pricelist.item']
        product = product._origin if product else product
        pricelist = self._client_pricelist(partner)
        if not (pricelist and product):
            return Item
        rule = Item.sudo().browse(
            pricelist.sudo()._get_product_rule(product, quantity or 1.0))
        if rule and rule.compute_price == 'fixed' \
                and rule.applied_on in ('0_product_variant', '1_product'):
            return rule
        return Item

    @api.model
    def _partner_products(self, partners):
        """{partner id: [(product, price for one unit)]} in one pass.

        For the phone's offline snapshot, which lists every priced client: the
        same rules `_rate_for` would pick, read once instead of per client.
        The price is display only -- the note is priced by `_rate_for` with
        the real quantity when it is issued.
        """
        company = self.env.company
        pricelist_of = {
            p.id: p.with_company(company).specific_property_product_pricelist
            for p in partners.sudo()
        }
        rules = self._rate_rules(self.env['product.pricelist'].union(*pricelist_of.values()))
        by_pricelist = rules.grouped('pricelist_id')
        result = {}
        for partner_id, pricelist in pricelist_of.items():
            pricelist_rules = by_pricelist.get(pricelist)
            if not pricelist_rules:
                continue
            entries = []
            for product in self._rule_products(pricelist_rules):
                # search() returned them in the model's _order, which is the
                # precedence Odoo's own rule selection walks.
                rule = next((r for r in pricelist_rules
                             if r._is_applicable_for(product, 1.0)), None)
                entries.append((product, rule.fixed_price if rule else 0.0))
            result[partner_id] = entries
        return result

    @api.model
    def _ensure_client_pricelist(self, partner):
        """The client's own pricelist, creating and assigning it if needed.

        One pricelist per client, so changing one client's price can never
        move another's. A pricelist already assigned to this client alone is
        adopted. One shared with other contacts is not edited: the client gets
        a copy of it, so every price he had before still applies.
        """
        partner = partner.commercial_partner_id.sudo()
        company = self.env.company
        Partner = self.env['res.partner'].sudo().with_company(company)
        current = partner.with_company(company).specific_property_product_pricelist
        if current and current.x_client_id == partner:
            return current
        if current and not current.x_client_id and Partner.search_count([
                ('specific_property_product_pricelist', '=', current.id)]) == 1:
            current.x_client_id = partner
            return current
        pricelist = self.env['product.pricelist'].sudo().create({
            'name': partner.name,
            'x_client_id': partner.id,
            'company_id': company.id,
            'currency_id': company.currency_id.id,
        })
        for item in current.item_ids:
            item.copy({'pricelist_id': pricelist.id})
        partner.with_company(company).specific_property_product_pricelist = pricelist
        return pricelist


class ProductPricelist(models.Model):
    _inherit = 'product.pricelist'

    x_client_id = fields.Many2one(
        'res.partner', string='Client', index=True, copy=False,
        help='The client this pricelist was made for. Water Delivery keeps one '
             'pricelist per client, so a change here never moves another '
             'client\'s price.',
    )


class ProductPricelistItem(models.Model):
    _inherit = 'product.pricelist.item'

    # Stored so Client Rates can group, filter and search by client. Writable
    # on create only: a new rate typed in Client Rates names the client, and
    # create() files it in that client's pricelist.
    x_client_id = fields.Many2one(
        'res.partner', string='Client', compute='_compute_x_client_id',
        store=True, readonly=False, precompute=True, index=True,
    )
    # Provenance. Rates are maintained in Odoo (the BAS import is a seed), so
    # what BAS last charged is kept only so the two can be compared.
    x_water_source = fields.Selection(
        [('bas', 'Imported from BAS9'), ('manual', 'Entered in Odoo')],
        string='Source', default='manual', copy=False,
    )
    x_bas_price = fields.Float(string='BAS Price at Import', readonly=True,
                               min_display_digits='Product Price', copy=False)
    x_bas_date = fields.Datetime(string='Last BAS Delivery', readonly=True, copy=False)
    x_bas_branch = fields.Char(string='BAS Branch (CODE2)', readonly=True, copy=False)
    x_differs_from_bas = fields.Boolean(
        string='Differs from BAS9', compute='_compute_differs_from_bas', store=True)

    @api.depends('pricelist_id.x_client_id')
    def _compute_x_client_id(self):
        for item in self:
            item.x_client_id = item.pricelist_id.x_client_id

    @api.depends('fixed_price', 'x_bas_price')
    def _compute_differs_from_bas(self):
        for item in self:
            item.x_differs_from_bas = bool(item.x_bas_price) and \
                abs(item.fixed_price - item.x_bas_price) > 1e-6

    @api.model_create_multi
    def create(self, vals_list):
        Pricing = self.env['ksw.water.pricing']
        Partner = self.env['res.partner']
        Product = self.env['product.product']
        for vals in vals_list:
            if not vals.get('x_client_id'):
                continue
            # Always the client's own pricelist, whatever default the form
            # proposed (core defaults pricelist_id to the company's first).
            vals['pricelist_id'] = Pricing._ensure_client_pricelist(
                Partner.browse(vals['x_client_id'])).id
            vals.setdefault('compute_price', 'fixed')
            if vals.get('product_id'):
                vals['product_tmpl_id'] = Product.browse(vals['product_id']).product_tmpl_id.id
                vals['applied_on'] = '0_product_variant'
            elif vals.get('product_tmpl_id'):
                vals['applied_on'] = '1_product'
        return super().create(vals_list)

    def write(self, vals):
        # A BAS rate somebody re-prices by hand is theirs from then on: the
        # next import records what BAS says but never overwrites it.
        if 'fixed_price' in vals and 'x_water_source' not in vals \
                and not self.env.context.get('ksw_bas_rate_import'):
            vals = dict(vals, x_water_source='manual')
        return super().write(vals)

    # ------------------------------------------------------------------
    # BAS code -> Odoo record. Two sources each, because the two databases
    # got their BAS records two different ways: dev from KSW_bas_gl_import
    # (`x_bas_code`, a module production does not have and should not get --
    # it drags in a whole GL import), production from the connector's own
    # mirror and a hand-kept Internal Reference.
    @api.model
    def _bas_partner_map(self):
        """FCODE -> res.partner."""
        result = {
            rec.bas_code.strip(): rec.partner_id
            for rec in self.env['ksw.bas.customer'].sudo().search([
                ('partner_id', '!=', False), ('bas_code', '!=', False)])
        }
        if 'x_bas_code' in self.env['res.partner']._fields:
            # The GL import's mapping is by account code, not by name, so
            # where both exist it wins over the mirror's name match.
            for partner in self.env['res.partner'].sudo().search(
                    [('x_bas_code', '!=', False)]):
                result[partner.x_bas_code.strip()] = partner
        return result

    @api.model
    def _bas_product_map(self):
        """ICODE -> product.product. Internal Reference is the standard
        place a product's code in another system goes, and the one a person
        can fill in without a module."""
        Product = self.env['product.product'].sudo()
        result = {
            p.default_code.strip(): p
            for p in Product.search([('default_code', '!=', False)])
            if p.default_code.strip()
        }
        if 'x_bas_code' in Product._fields:
            for product in Product.search([('x_bas_code', '!=', False)]):
                result[product.x_bas_code.strip()] = product
        return result

    @api.model
    def action_import_from_bas(self):
        """Seed the client pricelists from what BAS actually charged.

        Deliberately a button, not a cron: the decision recorded with the user
        was *import once, maintain in Odoo*. Re-running it is safe -- it never
        overwrites a rate somebody has edited here (`x_water_source ==
        'manual'`), and it refreshes the BAS provenance columns on the ones it
        owns, so the two systems can be compared without a second import path.
        """
        if not self.env.user.has_group('KSW_water_delivery.group_water_billing') \
                and not self.env.su:
            raise UserError(_('Only Water Delivery Billing may import rates.'))

        # Checked before connecting: with nothing to match against, a round
        # trip to the BAS server only ends in the same message.
        partners = self._bas_partner_map()
        products = self._bas_product_map()
        if not partners or not products:
            raise UserError(_(
                'Nothing to match the BAS rates against yet.\n\n'
                '%(partners)s BAS customers are linked to contacts: run '
                '"Match / Create Contacts" on BAS Customers first.\n'
                '%(products)s products carry a BAS item code: put the BAS item '
                'code in each water product\'s Internal Reference.',
                partners=len(partners), products=len(products),
            ))

        months = int(self.env['ir.config_parameter'].sudo().get_param(
            'ksw_water_delivery.rate_import_months', 12))
        conn = self.env['ksw.bas.connector']._bas_connect()
        try:
            cur = conn.cursor()
            cur.execute(_BAS_RATE_SQL % {'months': months})
            rows = cur.fetchall()
            cur.execute(_BAS_COVERAGE_SQL % {'months': months})
            coverage_rows = cur.fetchall()
        finally:
            conn.close()

        Product = self.env['product.product'].sudo()
        Item = self.sudo().with_context(ksw_bas_rate_import=True)
        Pricing = self.env['ksw.water.pricing']
        created = updated = skipped_manual = 0
        unmatched_clients, unmatched_items = set(), set()
        touched_products = Product.browse()

        for fcode, icode, price, unit, branch, fdate in rows:
            fcode = (fcode or '').strip()
            icode = (icode or '').strip()
            partner, product = partners.get(fcode), products.get(icode)
            if not partner:
                unmatched_clients.add(fcode)
                continue
            if not product:
                unmatched_items.add(icode)
                continue

            provenance = {
                'x_bas_price': price,
                'x_bas_date': fdate,
                'x_bas_branch': (branch or '').strip(),
            }
            pricelist = Pricing._ensure_client_pricelist(partner)
            rule = Item.search([
                ('pricelist_id', '=', pricelist.id), ('product_id', '=', product.id),
                *_RATE_RULE_DOMAIN,
            ], limit=1)
            if rule:
                if rule.x_water_source != 'bas':
                    # Somebody has taken ownership of this rate in Odoo. Record
                    # what BAS says for comparison, never overwrite the price.
                    rule.write(provenance)
                    skipped_manual += 1
                    continue
                rule.write(dict(provenance, fixed_price=price))
                updated += 1
            else:
                Item.create(dict(
                    provenance, x_client_id=partner.commercial_partner_id.id,
                    product_id=product.id, fixed_price=price, x_water_source='bas',
                ))
                created += 1
            touched_products |= product

        # A rated product is one this business sells by delivery, so its
        # invoicing policy has to match or month end would bill what was
        # ordered rather than what the driver actually delivered.
        stale = touched_products.filtered(lambda p: p.invoice_policy != 'delivery')
        if stale:
            stale.product_tmpl_id.sudo().write({'invoice_policy': 'delivery'})

        # The BAS-mapped products carry no sales tax at all -- KSW_bas_gl_import
        # created them from ITM10 for the ledger, where the tax sits on the
        # document rather than the item. Left alone, every delivery note would
        # print an after-tax total equal to its before-tax total, and the
        # monthly invoice would go out with no VAT on it. ITM10.ITAX is 15 on
        # every water item, which is the company's default sale tax.
        default_tax = self.env.company.account_sale_tax_id
        untaxed = touched_products.filtered(lambda p: not p.taxes_id)
        if untaxed and default_tax:
            untaxed.product_tmpl_id.sudo().write({'taxes_id': [(6, 0, default_tax.ids)]})
            _logger.info(
                'KSW_water_delivery: stamped %s on %s rated products that had '
                'no sales tax.', default_tax.name, len(untaxed),
            )

        # Branch coverage: which branch delivers to which client. This is what
        # cuts the driver's picker from 312 names to the 9-97 his branch
        # actually serves.
        Coverage = self.env['ksw.water.client.branch'].sudo()
        known = {
            (c.partner_id.id, c.branch_code): c
            for c in Coverage.with_context(active_test=False).search([])
        }
        cov_created = 0
        for fcode, branch, last_delivery in coverage_rows:
            partner = partners.get((fcode or '').strip())
            branch = (branch or '').strip()
            if not (partner and branch):
                continue
            row = known.get((partner.id, branch))
            if row:
                if row.source == 'bas':
                    row.write({'x_bas_last_delivery': last_delivery})
                continue
            Coverage.create({
                'partner_id': partner.id, 'branch_code': branch,
                'source': 'bas', 'x_bas_last_delivery': last_delivery,
            })
            cov_created += 1

        message = _(
            'Rates imported from BAS9.\n\n'
            '%(created)s created, %(updated)s refreshed, '
            '%(skipped)s left alone (edited in Odoo).\n'
            '%(products)s products switched to invoicing on delivered quantity, '
            '%(taxed)s given the default sales tax.\n'
            '%(coverage)s new client/branch pairs recorded.\n'
            'Unmatched client accounts: %(nc)s. Unmatched item codes: %(ni)s %(codes)s',
            created=created, updated=updated, skipped=skipped_manual,
            products=len(stale), taxed=len(untaxed) if default_tax else 0,
            coverage=cov_created,
            nc=len(unmatched_clients), ni=len(unmatched_items),
            # Named, because each one is a product somebody has to create
            # before its rates can come across -- a bare count says nothing
            # about what to do next.
            codes=('(%s)' % ', '.join(sorted(unmatched_items)[:25])) if unmatched_items else '',
        )
        _logger.info('KSW_water_delivery rate import: %s', message.replace('\n', ' '))
        if unmatched_clients:
            _logger.info('KSW_water_delivery unmatched BAS client accounts: %s',
                         ', '.join(sorted(unmatched_clients)))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('BAS Rate Import'),
                'message': message,
                'type': 'success',
                'sticky': True,
            },
        }
