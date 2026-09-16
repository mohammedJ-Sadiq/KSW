import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# BAS holds no pricelist table: ITM10.ISPRICE1..4 are zero on every item. The
# rate a client actually pays lives on each delivery-note line (str10.PRICE),
# and it is stable -- 322 of 389 (client, item) pairs were charged exactly one
# price over six months. So the register below is seeded from the newest price
# per pair rather than from a table that does not exist.
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


class KswWaterRate(models.Model):
    _name = 'ksw.water.rate'
    _description = 'Water Delivery Rate'
    _order = 'partner_id, product_id'
    _rec_names_search = ['partner_id', 'product_id']

    partner_id = fields.Many2one(
        'res.partner', string='Client', required=True, index=True,
        domain="[('customer_rank', '>', 0)]", ondelete='cascade',
    )
    product_id = fields.Many2one(
        'product.product', string='Product', required=True, index=True,
        domain="[('sale_ok', '=', True)]", ondelete='restrict',
    )
    price = fields.Float(
        string='Rate', required=True, digits='Product Price',
        help='Price per unit of the product, before VAT.',
    )
    uom_name = fields.Char(related='product_id.uom_id.name', string='Per')
    currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id,
        required=True,
    )
    company_id = fields.Many2one(
        'res.company', default=lambda self: self.env.company, required=True,
    )
    active = fields.Boolean(default=True)

    # Provenance. The rate is maintained in Odoo from here on (the import is a
    # one-off seed), so what BAS last said is kept only so the two can be
    # compared later -- an imported rate that nobody has since touched still
    # shows where it came from.
    source = fields.Selection(
        [('bas', 'Imported from BAS9'), ('manual', 'Entered in Odoo')],
        default='manual', required=True, readonly=True,
    )
    x_bas_price = fields.Float(string='BAS Price at Import', readonly=True, digits='Product Price')
    x_bas_date = fields.Datetime(string='Last BAS Delivery', readonly=True)
    x_bas_branch = fields.Char(string='BAS Branch (CODE2)', readonly=True)

    _partner_product_uniq = models.Constraint(
        'unique(partner_id, product_id, company_id)',
        'This client already has a rate for this product.',
    )

    @api.depends('partner_id', 'product_id', 'price')
    def _compute_display_name(self):
        for rate in self:
            rate.display_name = '%s — %s' % (
                rate.partner_id.display_name or '', rate.product_id.display_name or '',
            )

    @api.constrains('price')
    def _check_price(self):
        for rate in self:
            if rate.price <= 0:
                raise UserError(_('A rate has to be greater than zero.'))

    # ------------------------------------------------------------------
    @api.model
    def _rated_partner_ids(self):
        """Clients the drivers may deliver to: exactly those we can price."""
        self.env.cr.execute("""
            SELECT DISTINCT partner_id FROM ksw_water_rate
             WHERE active = TRUE AND company_id = %s
        """, (self.env.company.id,))
        return [row[0] for row in self.env.cr.fetchall()]

    @api.model
    def _products_for_partner(self, partner):
        # `_origin`: read from an unsaved form record (an onchange), a
        # relational value comes back NewId-wrapped, and searching on a NewId
        # matches nothing and raises nothing -- the picker just comes back
        # empty. Resolve to the real record before every search.
        partner = partner._origin if partner else partner
        if not partner:
            return self.env['product.product']
        rates = self.sudo().search([('partner_id', '=', partner.id)])
        return rates.product_id

    @api.model
    def _rate_for(self, partner, product):
        partner = partner._origin if partner else partner
        product = product._origin if product else product
        if not (partner and product):
            return self.browse()
        return self.sudo().search([
            ('partner_id', '=', partner.id), ('product_id', '=', product.id),
        ], limit=1)

    # ------------------------------------------------------------------
    def action_import_from_bas(self):
        """One-off seed of the register from what BAS actually charged.

        Deliberately a button, not a cron: the decision recorded with the user
        was *import once, maintain in Odoo*. Re-running it is safe -- it never
        overwrites a rate somebody has edited here (`source == 'manual'`), and
        it refreshes the BAS provenance columns on the ones it owns, so the two
        systems can be compared without a second import path existing.
        """
        if not self.env.user.has_group('KSW_water_delivery.group_water_billing') \
                and not self.env.su:
            raise UserError(_('Only Water Delivery Billing may import rates.'))

        # The import matches BAS accounts and item codes to Odoo records through
        # `x_bas_code`, which is declared by KSW_bas_gl_import -- a module this
        # one deliberately does NOT depend on, because everything else here
        # works without it and it drags in a whole GL import. So the dependency
        # is checked at the one moment it is needed, with a message that says
        # what to do, rather than surfacing as "Invalid field 'x_bas_code'".
        missing = [
            model for model in ('res.partner', 'product.product')
            if 'x_bas_code' not in self.env[model]._fields
        ]
        if missing:
            raise UserError(_(
                'Rates are matched to BAS by the BAS account and item codes '
                '(`x_bas_code`), which are not present on %(models)s in this '
                'database. Install KSW_bas_gl_import and import the chart and '
                'items first, or enter the rates by hand.',
                models=', '.join(missing),
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

        Partner = self.env['res.partner'].sudo()
        Product = self.env['product.product'].sudo()
        partners = {
            p.x_bas_code.strip(): p
            for p in Partner.search([('x_bas_code', '!=', False)]) if p.x_bas_code
        }
        products = {
            p.x_bas_code.strip(): p
            for p in Product.search([('x_bas_code', '!=', False)]) if p.x_bas_code
        }

        existing = {
            (r.partner_id.id, r.product_id.id): r
            for r in self.sudo().with_context(active_test=False).search([])
        }

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

            vals = {
                'price': price,
                'x_bas_price': price,
                'x_bas_date': fdate,
                'x_bas_branch': (branch or '').strip(),
                'source': 'bas',
            }
            rate = existing.get((partner.id, product.id))
            if rate:
                if rate.source == 'manual':
                    # Somebody has taken ownership of this rate in Odoo. Record
                    # what BAS says for comparison, never overwrite the price.
                    rate.sudo().write({
                        'x_bas_price': price,
                        'x_bas_date': fdate,
                        'x_bas_branch': (branch or '').strip(),
                    })
                    skipped_manual += 1
                    continue
                rate.sudo().write(vals)
                updated += 1
            else:
                self.sudo().create(dict(
                    vals, partner_id=partner.id, product_id=product.id,
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
            'Unmatched client accounts: %(nc)s. Unmatched item codes: %(ni)s.',
            created=created, updated=updated, skipped=skipped_manual,
            products=len(stale), taxed=len(untaxed) if default_tax else 0,
            coverage=cov_created,
            nc=len(unmatched_clients), ni=len(unmatched_items),
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
