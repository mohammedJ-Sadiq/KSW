import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Which revenue account BAS credits for a credit delivery note. The customer's
# own VOU10 line carries no counter-account; the revenue is the same
# document's credit line (TCODE 4*). It is decided by branch x item, not item
# alone: 11032 posts to eleven accounts, one per branch ("sales, branch X,
# trailers"), while (CODE2, ICODE) lands on one account for 99.85% of lines
# (verified 2026-10-04). The few strays are counted, not followed.
_BAS_REVENUE_SQL = """
    SELECT s.CODE2, s.ICODE, r.TCODE, COUNT(*) AS n
      FROM VOU10 v
      JOIN VOU10 r ON r.FTYPE = v.FTYPE AND r.FTYPE2 = v.FTYPE2
                  AND r.CODE2 = v.CODE2 AND r.NUMBER1 = v.NUMBER1
                  AND r.TCODE LIKE '4%%'
      JOIN STR10 s ON s.FTYPE = v.FTYPE AND s.FTYPE2 = v.FTYPE2
                  AND s.CODE2 = v.CODE2 AND s.NUMBER1 = v.NUMBER1
     WHERE v.FTYPE = '600' AND v.FCODE LIKE '1203%%'
       AND v.FDATE >= DATEADD(month, -%(months)s, GETDATE())
     GROUP BY s.CODE2, s.ICODE, r.TCODE
"""


class KswWaterRevenueAccount(models.Model):
    """Branch x product -> revenue account, the way BAS posts it.

    A product-level income account cannot express this: the same water from
    two branches belongs in two accounts. Seeded from BAS, then maintained
    here, like the rate register.
    """
    _name = 'ksw.water.revenue.account'
    _description = 'Water Delivery Revenue Account'
    _order = 'branch_code, product_id'

    branch_code = fields.Char(string='Branch (CODE2)', required=True, index=True)
    product_id = fields.Many2one(
        'product.product', string='Product', required=True, index=True,
        ondelete='cascade',
    )
    # Empty when BAS's account does not exist in this chart of accounts: the
    # row is kept so the gap is visible, and the line falls back to core.
    account_id = fields.Many2one(
        'account.account', string='Revenue Account',
        domain="[('account_type', 'in', ('income', 'income_other'))]",
    )
    company_id = fields.Many2one(
        'res.company', default=lambda self: self.env.company, required=True,
    )
    source = fields.Selection(
        [('bas', 'Imported from BAS9'), ('manual', 'Entered in Odoo')],
        default='manual', required=True, readonly=True,
    )
    x_bas_account_code = fields.Char(string='BAS Account', readonly=True)
    x_bas_share = fields.Float(
        string='BAS Lines on It (%)', readonly=True, digits=(5, 2),
        help='Share of BAS lines for this branch and product that went to '
             'this account. Below 100 means BAS occasionally posted elsewhere.',
    )

    _branch_product_uniq = models.Constraint(
        'unique(branch_code, product_id, company_id)',
        'This branch already has a revenue account for this product.',
    )

    def write(self, vals):
        # Changing the account by hand takes the row over, so a later import
        # refreshes the BAS columns only.
        if 'account_id' in vals and not self.env.context.get('ksw_water_bas_import'):
            vals = dict(vals, source='manual')
        return super().write(vals)

    @api.model
    def _account_for(self, branch_code, product):
        if not (branch_code and product):
            return self.env['account.account']
        return self.sudo().search([
            ('branch_code', '=', branch_code), ('product_id', '=', product.id),
            ('company_id', '=', self.env.company.id), ('account_id', '!=', False),
        ], limit=1).account_id

    def action_import_from_bas(self):
        if not self.env.user.has_group('KSW_water_delivery.group_water_billing') \
                and not self.env.su:
            raise UserError(_('Only Water Delivery Billing may import revenue accounts.'))

        products = self.env['ksw.water.rate']._bas_product_map()
        months = int(self.env['ir.config_parameter'].sudo().get_param(
            'ksw_water_delivery.rate_import_months', 12))
        conn = self.env['ksw.bas.connector']._bas_connect()
        try:
            cur = conn.cursor()
            cur.execute(_BAS_REVENUE_SQL % {'months': months})
            rows = cur.fetchall()
        finally:
            conn.close()

        # Dominant account per (branch, item).
        totals, best = {}, {}
        for branch, icode, tcode, n in rows:
            key = ((branch or '').strip(), (icode or '').strip())
            totals[key] = totals.get(key, 0) + n
            if n > best.get(key, ('', 0))[1]:
                best[key] = ((tcode or '').strip(), n)

        Account = self.env['account.account'].sudo()
        accounts = {}
        existing = {(r.branch_code, r.product_id.id): r for r in self.sudo().search([])}
        created = updated = no_account = 0
        unmatched_items = set()
        for (branch, icode), (tcode, n) in best.items():
            product = products.get(icode)
            if not (branch and product):
                unmatched_items.add(icode)
                continue
            if tcode not in accounts:
                accounts[tcode] = Account.search([('code', '=', tcode)], limit=1)
            account = accounts[tcode]
            no_account += not account
            vals = {
                'x_bas_account_code': tcode,
                'x_bas_share': 100.0 * n / totals[(branch, icode)],
            }
            row = existing.get((branch, product.id))
            if row and row.source == 'manual':
                row.sudo().write(vals)
                continue
            vals.update({'account_id': account.id or False, 'source': 'bas'})
            if row:
                row.sudo().with_context(ksw_water_bas_import=True).write(vals)
                updated += 1
            else:
                self.sudo().create(dict(vals, branch_code=branch, product_id=product.id))
                created += 1

        message = _(
            'Revenue accounts imported from BAS9: %(created)s created, '
            '%(updated)s refreshed. %(missing)s branch/product pairs point at a '
            'BAS account this chart of accounts does not have. Unmatched item '
            'codes: %(items)s',
            created=created, updated=updated, missing=no_account,
            items=', '.join(sorted(unmatched_items)) or '-',
        )
        _logger.info('KSW_water_delivery revenue account import: %s', message)
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': _('BAS Revenue Accounts'), 'message': message,
                       'type': 'success', 'sticky': True},
        }


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    def _prepare_invoice_line(self, **optional_values):
        """A water note's line posts to its branch's revenue account.

        Hooked here rather than in the month-end builder so that Odoo's own
        "Create Invoice" on the orders gets the same account.
        """
        res = super()._prepare_invoice_line(**optional_values)
        if self.display_type or not self.product_id or 'account_id' in optional_values:
            return res
        note = self.order_id.picking_ids.filtered('x_is_water_delivery')[:1]
        if note:
            account = self.env['ksw.water.revenue.account']._account_for(
                note.x_branch_code, self.product_id)
            if account:
                res['account_id'] = account.id
        return res
