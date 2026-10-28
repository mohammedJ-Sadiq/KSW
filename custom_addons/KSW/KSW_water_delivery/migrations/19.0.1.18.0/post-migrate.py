"""Move every water rate onto its client's own pricelist (Oct 2026).

ksw.water.rate was a second price register next to Odoo's pricelists: the
delivery notes priced from it, sales orders from the pricelist, and nothing
compared the two. Each active rate becomes a fixed-price rule on the client's
own pricelist (created and assigned if needed, see
ksw.water.pricing._ensure_client_pricelist). Where the pricelist already had a
fixed price for the same product, the rate wins: it is what the delivery notes
have actually been billed at. Each such case is logged.

Read with SQL: the model is gone from the registry by now, and its table is
only dropped at the end of the upgrade, after this script has run.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("SELECT to_regclass('ksw_water_rate')")
    if not cr.fetchone()[0]:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    cr.execute("""
        SELECT partner_id, product_id, price, company_id, active, source,
               x_bas_price, x_bas_date, x_bas_branch
          FROM ksw_water_rate
         ORDER BY company_id, partner_id, id
    """)
    rows = cr.dictfetchall()

    moved = replaced = inactive = 0
    for row in rows:
        if not row['active']:
            inactive += 1
            continue
        company = env['res.company'].browse(row['company_id'])
        partner = env['res.partner'].browse(row['partner_id']).commercial_partner_id
        product = env['product.product'].browse(row['product_id'])
        Item = env['product.pricelist.item'].with_company(company).with_context(
            ksw_bas_rate_import=True)
        pricelist = env['ksw.water.pricing'].with_company(company) \
            ._ensure_client_pricelist(partner)
        vals = {
            'fixed_price': row['price'],
            'x_water_source': row['source'] or 'manual',
            'x_bas_price': row['x_bas_price'] or 0.0,
            'x_bas_date': row['x_bas_date'],
            'x_bas_branch': row['x_bas_branch'],
        }
        existing = Item.search([
            ('pricelist_id', '=', pricelist.id),
            ('compute_price', '=', 'fixed'),
            ('min_quantity', '<=', 1),
            ('date_start', '=', False), ('date_end', '=', False),
            '|', ('product_id', '=', product.id),
            '&', ('applied_on', '=', '1_product'),
            ('product_tmpl_id', '=', product.product_tmpl_id.id),
        ])
        if existing:
            for item in existing:
                if abs(item.fixed_price - row['price']) > 1e-6:
                    _logger.warning(
                        'KSW_water_delivery: %s / %s: pricelist %s said %s, the '
                        'water rate %s. Kept the water rate.',
                        partner.display_name, product.display_name,
                        pricelist.display_name, item.fixed_price, row['price'])
            existing.write(vals)
            replaced += 1
        else:
            Item.create(dict(vals, x_client_id=partner.id, product_id=product.id))
        moved += 1

    # Sales orders read a partner's pricelist only with this setting on
    # (product.pricelist._get_partner_pricelist_multi); without it the client
    # pricelists would price nothing outside the water module.
    group_user = env.ref('base.group_user')
    pricelist_group = env.ref('product.group_product_pricelist')
    if pricelist_group not in group_user.implied_ids:
        group_user.write({'implied_ids': [(4, pricelist_group.id)]})

    _logger.info(
        'KSW_water_delivery: %s water rates moved onto client pricelists '
        '(%s onto an existing rule), %s inactive rates not carried over.',
        moved, replaced, inactive)
