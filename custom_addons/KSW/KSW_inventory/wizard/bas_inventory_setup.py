import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# BAS item code prefix -> (category xmlid, stock account, default issue account).
# Read off BAS itself: every 2026 issue (vou10 FTYPE 101) of a 15xxx item
# credits 1206010002, of a 31xxx item 1206010001, of a 33xxx item 1206010003,
# with no exception; tyres are issued mostly to 3102020002, desalination
# material to 3102010001.
STOCK_CLASSES = {
    '15': ('KSW_inventory.categ_desal_parts', '1206010002', '3102010001'),
    '31': ('KSW_inventory.categ_membranes', '1206010001', '3102010001'),
    '33': ('KSW_inventory.categ_tyres', '1206010003', '3102020002'),
}
WATER_PREFIXES = ('00', '10', '11', '12', '13', '30', '44', '53', '61', '70')
SERVICE_PREFIXES = ('14', '41', '51', '52', '60', '66', '81', '90')

UOM_BY_BAS_UNIT = {
    'cubic m': 'uom.product_uom_cubic_meter', 'متر مكعب': 'uom.product_uom_cubic_meter',
    'cubic meters': 'uom.product_uom_cubic_meter',
    'day': 'uom.product_uom_day', 'hour': 'uom.product_uom_hour',
    'gallon': 'uom.product_uom_gal',
}


class KswBasInventorySetup(models.TransientModel):
    """Inventory from BAS: warehouses, item classes, issue operations, opening stock."""

    _name = 'ksw.bas.inventory.setup'
    _inherit = ['ksw.bas.connector']
    _description = 'BAS Inventory Setup'

    company_id = fields.Many2one('res.company', required=True, default=lambda s: s.env.company)
    log = fields.Text(readonly=True)

    # ------------------------------------------------------------------ helpers
    def _account(self, code):
        acc = self.env['account.account'].with_company(self.company_id).search(
            [('code', '=', code)], limit=1)
        if not acc:
            raise UserError(self.env._("Account %s is missing from the chart; run the BAS GL import first.", code))
        return acc

    def _tax(self, use):
        return self.env['account.tax'].search([
            ('company_id', '=', self.company_id.id), ('type_tax_use', '=', use),
            ('amount', '=', 15.0), ('amount_type', '=', 'percent'), ('price_include', '=', False),
            ('name', '=', '15%')], order='id', limit=1)

    @staticmethod
    def _classify(code):
        code = (code or '').strip()
        if code[:2] in STOCK_CLASSES and len(code) >= 5:
            return 'stock', STOCK_CLASSES[code[:2]]
        if code == '666' or code[:2] in SERVICE_PREFIXES:
            return 'service', 'KSW_inventory.categ_services'
        if code[:2] in WATER_PREFIXES:
            return 'water', 'KSW_inventory.categ_water'
        return False, False

    def _fetch(self, sql, params=()):
        conn = self._bas_connect()
        try:
            cur = conn.cursor(as_dict=True)
            cur.execute(sql, params)
            return cur.fetchall()
        finally:
            conn.close()

    def _done(self, lines):
        self.log = '\n'.join(lines)
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'new'}

    # ------------------------------------------------------------------ steps
    def _setup_valuation(self, out):
        """Item classes: perpetual, weighted average, BAS stock + issue accounts."""
        company = self.company_id
        bas_names = {r['c']: (r['n'] or '').strip() for r in self._fetch(
            "SELECT RTRIM(DCODE1) c, RTRIM(DNAME) n FROM COD10 WHERE DCODE1 IN %s",
            (tuple(v[1] for v in STOCK_CLASSES.values()),))}
        for categ_xmlid, stock_code, issue_code in STOCK_CLASSES.values():
            stock_acc = self._account(stock_code)
            # BAS renamed 1206010003 to tyres after the chart was imported; the
            # 2026 vouchers already post tyres to it, so the label follows.
            if bas_names.get(stock_code) and (stock_acc.x_bas_name_ar or '').strip() != bas_names[stock_code]:
                stock_acc.update_field_translations('name', {'en_US': bas_names[stock_code], 'ar_001': bas_names[stock_code]})
                stock_acc.x_bas_name_ar = bas_names[stock_code]
                out.append(f"Renamed {stock_code} to BAS's current name: {bas_names[stock_code]}")
            self.env.ref(categ_xmlid).with_company(company).write({
                'property_valuation': 'real_time',
                'property_cost_method': 'average',
                'property_stock_valuation_account_id': stock_acc.id,
                'property_account_expense_categ_id': self._account(issue_code).id,
            })
        consumption = self.env.ref('KSW_inventory.location_consumption')
        if not consumption.valuation_account_id:
            consumption.valuation_account_id = self._account('3102010001')
        if not company.account_stock_journal_id:
            raise UserError(self.env._("The company has no Stock Journal (Inventory Valuation)."))
        # Cost centres must be visible to whoever issues stock (they are required).
        analytic = self.env.ref('analytic.group_analytic_accounting')
        user_group = self.env.ref('base.group_user')
        if analytic not in user_group.implied_ids:
            user_group.write({'implied_ids': [(4, analytic.id)]})
            out.append("Analytic accounting (cost centres) enabled for internal users")
        out.append("Item classes: perpetual valuation, weighted average, BAS stock and issue accounts")

    def _sync_items(self, out):
        """Classify BAS items, create the missing ones, fix taxes on stock items."""
        rows = self._fetch("""SELECT RTRIM(ICODE) code, RTRIM(IDSCR) name, RTRIM(ISNULL(IUNIT,'')) unit,
                                     ICPRICE cost, STOP_ITM stop FROM ITM10""")
        Product = self.env['product.template'].with_context(active_test=False)
        existing = {p.default_code.strip(): p for p in Product.search([('default_code', '!=', False)])}
        sale_tax, purchase_tax = self._tax('sale'), self._tax('purchase')
        created = classified = 0
        for r in rows:
            kind, cls = self._classify(r['code'])
            if not kind:
                continue
            categ = self.env.ref(cls[0] if kind == 'stock' else cls)
            tmpl = existing.get(r['code'])
            if not tmpl:
                uom = self.env.ref(UOM_BY_BAS_UNIT.get(r['unit'].lower(), 'uom.product_uom_unit'))
                tmpl = Product.create({
                    'name': r['name'] or r['code'], 'default_code': r['code'],
                    'type': 'service' if kind == 'service' else 'consu',
                    'is_storable': kind == 'stock', 'categ_id': categ.id,
                    'uom_id': uom.id, 'active': not r['stop'],
                })
                created += 1
            elif tmpl.categ_id != categ and (not tmpl.categ_id or tmpl.categ_id == self.env.ref('product.product_category_all')
                                             or kind == 'stock'):
                tmpl.categ_id = categ
                classified += 1
            if kind == 'stock':
                vals = {'is_storable': True, 'purchase_method': 'receive'}
                if purchase_tax and not tmpl.supplier_taxes_id.filtered(lambda t: t.company_id == self.company_id):
                    vals['supplier_taxes_id'] = [(4, purchase_tax.id)]
                if sale_tax and not tmpl.taxes_id.filtered(lambda t: t.company_id == self.company_id):
                    vals['taxes_id'] = [(4, sale_tax.id)]
                tmpl.write(vals)
        out.append(f"Items: {created} created from BAS, {classified} classified; stock items bill on received quantity, VAT 15%")

    def _import_warehouses(self, out):
        """BAS WSHOW, company 10: FTYPE 1 warehouses, FTYPE 2 showrooms. Create-only."""
        rows = self._fetch("""SELECT RTRIM(CODE) code, RTRIM(NAME) name, FTYPE kind FROM WSHOW
                              WHERE RTRIM(BRAN) = '10' AND FTYPE IN ('1', '2') ORDER BY FTYPE, CODE""")
        Warehouse = self.env['stock.warehouse'].with_context(active_test=False)
        have = {(w.x_bas_kind, w.x_bas_code) for w in Warehouse.search([('x_bas_code', '!=', False)])}
        created = 0
        for r in rows:
            kind = 'warehouse' if r['kind'].strip() == '1' else 'showroom'
            if (kind, r['code']) in have:
                continue
            name = r['name'] if kind == 'warehouse' else f"معرض {r['code']} - {r['name']}"
            Warehouse.create({
                'name': name, 'code': ('W' if kind == 'warehouse' else 'S') + r['code'],
                'company_id': self.company_id.id, 'x_bas_code': r['code'], 'x_bas_kind': kind,
            })
            created += 1
        out.append(f"Warehouses: {created} created from BAS ({len(rows)} in BAS)")

    def _setup_issue_types(self, out):
        consumption = self.env.ref('KSW_inventory.location_consumption')
        Type = self.env['stock.picking.type'].with_context(active_test=False)
        created = 0
        for wh in self.env['stock.warehouse'].search([('x_bas_code', '!=', False), ('company_id', '=', self.company_id.id)]):
            if Type.search_count([('warehouse_id', '=', wh.id), ('x_ksw_issue', '=', True)]):
                continue
            ptype = Type.create({
                'name': 'Stock Issue', 'code': 'outgoing', 'sequence_code': 'ISS',
                'warehouse_id': wh.id, 'company_id': self.company_id.id,
                'default_location_src_id': wh.lot_stock_id.id,
                'default_location_dest_id': consumption.id, 'x_ksw_issue': True,
            })
            ptype.update_field_translations('name', {'ar_001': 'صرف مخزون'})
            created += 1
        out.append(f"Stock Issue operations: {created} created")

    # ------------------------------------------------------------------ actions
    def action_run_setup(self):
        self.ensure_one()
        out = []
        self._setup_valuation(out)
        self._sync_items(out)
        self._import_warehouses(out)
        self._setup_issue_types(out)
        return self._done(out)

    def action_load_opening_stock(self):
        """Put BAS's item register into Odoo, per warehouse, at BAS unit cost.

        Posts NOTHING: the ledger already holds BAS's 1206 balances (from the
        GL import), so an opening that posted would count the stock twice.
        Stock classes are switched to periodic for the load and back after.
        Quantities elsewhere for these items (an earlier seed into WH/Stock)
        are set to zero. Re-running sets the same quantities again.
        """
        self.ensure_one()
        company = self.company_id
        Warehouse = self.env['stock.warehouse']
        whs = {(w.x_bas_kind, w.x_bas_code): w for w in Warehouse.search(
            [('x_bas_code', '!=', False), ('company_id', '=', company.id)])}
        if not whs:
            raise UserError(self.env._("Run the setup first: no BAS warehouses yet."))
        conn = self._bas_connect()
        try:
            cur = conn.cursor(as_dict=True)
            qty = {}
            for table, kind, digit in (('ITMW10', 'warehouse', '1'), ('ITMS10', 'showroom', '2')):
                cur.execute(f"SELECT COLUMN_NAME c FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='{table}' AND COLUMN_NAME LIKE 'IAQ%%'")
                cols = [r['c'] for r in cur.fetchall()]
                cur.execute(f"SELECT RTRIM(ICODE) code, {', '.join(cols)} FROM {table}")
                for r in cur.fetchall():
                    for c in cols:
                        if r[c] and c[3:4] == digit:
                            qty[(r['code'], kind, c[4:])] = float(r[c])
            cur.execute("SELECT RTRIM(ICODE) code, ICPRICE cost FROM ITM10")
            cost = {r['code']: float(r['cost'] or 0.0) for r in cur.fetchall()}
        finally:
            conn.close()

        products = {p.default_code.strip(): p for p in self.env['product.product'].search(
            [('default_code', '!=', False), ('is_storable', '=', True)])}
        categs = self.env['product.category'].browse(
            [self.env.ref(c).id for c, _a, _b in STOCK_CLASSES.values()]).with_company(company)
        out, skipped = [], []
        Quant = self.env['stock.quant'].with_context(inventory_mode=True)
        categs.write({'property_valuation': 'periodic'})
        try:
            targets = {}
            for (code, kind, wcode), q in qty.items():
                product, wh = products.get(code), whs.get((kind, wcode))
                if not product or not wh:
                    skipped.append(f"{code}@{kind}{wcode}" + ("" if product else " (not a stock item)"))
                    continue
                if q < 0:
                    skipped.append(f"{code}@{wh.code} negative {q}")
                    continue
                targets[(product.id, wh.lot_stock_id.id)] = q
            for product in products.values():
                if cost.get(product.default_code.strip()):
                    product.with_company(company).standard_price = cost[product.default_code.strip()]
            # Zero whatever sits elsewhere, then set BAS's quantities.
            for quant in Quant.search([('product_id', 'in', [p.id for p in products.values()]),
                                       ('location_id.usage', '=', 'internal'), ('company_id', '=', company.id)]):
                if (quant.product_id.id, quant.location_id.id) not in targets and quant.quantity:
                    quant.inventory_quantity = 0
                    quant.action_apply_inventory()
            for (pid, lid), q in targets.items():
                quant = Quant.search([('product_id', '=', pid), ('location_id', '=', lid)], limit=1) \
                    or Quant.create({'product_id': pid, 'location_id': lid})
                if quant.quantity != q:
                    quant.inventory_quantity = q
                    quant.action_apply_inventory()
        finally:
            categs.write({'property_valuation': 'real_time'})
        value = sum(p.qty_available * p.standard_price for p in products.values())
        out.append(f"Opening stock: {len(targets)} item/warehouse quantities set from BAS, value {value:,.2f} at BAS unit cost; nothing posted")
        if skipped:
            out.append("Skipped: " + ", ".join(sorted(skipped)[:40]))
        return self._done(out)
