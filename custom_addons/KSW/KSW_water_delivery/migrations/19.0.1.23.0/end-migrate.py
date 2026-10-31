"""The branch master becomes the warehouse (Oct 2026). See models/water_branch.py.

Each water operation type moves into its branch's own warehouse, found by BAS's
rule (branch code = "1" + warehouse code: 120 -> W20), and the branch fields it
held (code, sales account, cost centre, cash customer, till, cash sales
journal) move onto that warehouse. They are read from the operation type's old
columns: the fields are related to the warehouse now, the columns remain.

end-migrate, not post: the warehouses' BAS codes (`x_bas_code`) belong to
KSW_inventory and the branch names to KSW_bas_gl_import, neither a dependency
(KSW gotcha #45).

Refuses (aborting the upgrade) when two branches would share one warehouse
with different settings: that is a database without a warehouse per branch
(production before its inventory setup), and folding twelve branches into one
warehouse would lose their settings. Set the warehouses up first.
"""
import logging
from collections import defaultdict

from odoo import SUPERUSER_ID, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

BRANCH_COLUMNS = ('x_branch_code', 'x_sales_account_id', 'x_analytic_account_id',
                  'x_cash_partner_id', 'x_cash_journal_id', 'x_cash_sale_journal_id')


def migrate(cr, version):
    cr.execute("""
        SELECT column_name FROM information_schema.columns
         WHERE table_name = 'stock_picking_type' AND column_name IN %s
    """, [BRANCH_COLUMNS])
    cols = sorted(r[0] for r in cr.fetchall())
    if 'x_branch_code' not in cols:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    cr.execute("SELECT id, warehouse_id, default_location_src_id, %s FROM stock_picking_type "
               "WHERE x_is_water_delivery = TRUE" % ', '.join(cols))
    rows = cr.dictfetchall()
    Warehouse = env['stock.warehouse'].with_context(active_test=False)
    Type = env['stock.picking.type'].with_context(active_test=False)
    has_bas = 'x_bas_code' in Warehouse._fields

    target = {}
    for row in rows:
        code = (row['x_branch_code'] or '').strip()
        warehouse = Warehouse.browse(row['warehouse_id'])
        if has_bas and len(code) == 3:
            warehouse = Warehouse.search([('x_bas_code', '=', code[1:])], limit=1) or warehouse
        target[row['id']] = warehouse

    shared = defaultdict(list)
    for row in rows:
        shared[target[row['id']].id].append(row)
    for warehouse_id, branch_rows in shared.items():
        if len({tuple(r[c] for c in cols) for r in branch_rows}) > 1:
            names = ', '.join(Type.browse(r['id']).name for r in branch_rows)
            raise UserError(
                'Water branches %s would share warehouse %s with different settings. '
                'Give every branch its own warehouse first (the BAS warehouses of '
                'KSW_inventory), then upgrade again.' % (names, Warehouse.browse(warehouse_id).name))

    moved = 0
    for row in rows:
        ptype, warehouse = Type.browse(row['id']), target[row['id']]
        vals = {}
        if ptype.warehouse_id != warehouse:
            old_stock = ptype.warehouse_id.lot_stock_id
            vals['warehouse_id'] = warehouse.id
            if row['default_location_src_id'] == old_stock.id:
                vals['default_location_src_id'] = warehouse.lot_stock_id.id
            moved += 1
        if vals:
            ptype.write(vals)
        warehouse.write({c: row[c] or False for c in cols})

    # The four branches created unnamed ("Water Delivery Notes 135") take
    # their BAS name.
    if 'ksw.bas.branch' in env:
        Branch = env['ksw.bas.branch'].with_context(active_test=False)
        for ptype in Type.browse([r['id'] for r in rows]):
            code = ptype.warehouse_id.x_branch_code
            if code and (ptype.with_context(lang='en_US').name or '').startswith('Water Delivery Notes'):
                branch = Branch.search([('code', '=', code)], limit=1)
                if branch:
                    ptype.with_context(lang='en_US').name = branch.with_context(lang='en_US').name
                    arabic = branch.with_context(lang='ar_001').name
                    if arabic and env['res.lang']._lang_get('ar_001'):
                        ptype.update_field_translations('name', {'ar_001': arabic})

    for model in ('ksw.water.client.branch', 'ksw.water.revenue.account'):
        records = env[model].with_context(active_test=False).search([])
        env.add_to_compute(records._fields['warehouse_id'], records)
        records.flush_model(['warehouse_id'])
    _logger.info('KSW_water_delivery: %s water branches moved to their own warehouse; '
                 'branch settings now on the warehouse.', moved)
