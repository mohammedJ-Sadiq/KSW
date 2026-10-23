"""The 2026 stock history, rebuilt from BAS's own item lines.

BAS keeps item lines for exactly two document types, in tables the first
import never read:

* ``din10`` — purchases (``006``): item, quantity, price, per bill;
* ``bin10`` FTYPE ``101`` — stock issues: item, quantity, the vehicle
  (``COST_CENTER``) and the account charged (``FCODE``).

Nothing else moves the stock items (checked: no other document type touches
them), so for an item and warehouse BAS's balance today must equal
opening + purchases − issues. The two actions here use that:

**Link Purchases from BAS** fills the purchase history from ``din10``, exactly:
the bill's stock line gets the item(s) and quantities BAS recorded. It covers
what text matching could not (lines with two items, spelling differences) and
the purchases that were imported as journal entries rather than bills.

**Rebuild Movement History** replays the year as dated stock moves: one
receipt per BAS purchase, one issue per BAS issue (to the consumption location,
with the vehicle), and one *BAS opening balance* adjustment on 1 January.
The existing opening-load lines are re-dated to 1 January too. The opening
adjustment is (issues − purchases), so **today's stock does not change by a
single unit**: the history is added, the balance is not.

Two rules carried over from the opening load:

* **Nothing is posted.** The ledger already holds these purchases and issues
  (BAS GL import). Stock classes are periodic while the replay runs, and real
  time again afterwards; unit costs are put back to BAS's.
* **An item whose BAS figures do not add up is not replayed**, it is reported.
  A negative opening means BAS's own register disagrees with its documents
  (31001, 33542 in 2026), and inventing a movement to hide that would be a lie
  in the stock history.

Re-running is safe: a BAS document already replayed (``stock.picking.x_bas_key``)
is skipped, and so is an item that already has its opening balance.
"""
import re
from collections import defaultdict
from datetime import date, datetime, time

from odoo import fields, models
from odoo.exceptions import UserError

from .bas_inventory_setup import STOCK_CLASSES

_OPENING_NAME = 'BAS opening balance %s'


def _key(r):
    return '%s/%s/%s/%s' % (r['FTYPE'], r['FTYPE2'], r['CODE2'], r['N'])


def _norm(name):
    return re.sub(r'\s+', ' ', (name or '').replace('\xa0', ' ')).strip().lower()


class KswBasInventorySetup(models.TransientModel):
    _inherit = 'ksw.bas.inventory.setup'

    year = fields.Integer(default=lambda s: fields.Date.context_today(s).year,
                          help='The BAS fiscal year to replay (din10 / bin10 hold the current year).')

    # ------------------------------------------------------------------ BAS
    def _bas_item_lines(self):
        cols = ("FTYPE, FTYPE2, RTRIM(CODE2) CODE2, CAST(NUMBER1 AS bigint) N, SERIAL, FDATE, "
                "RTRIM(ICODE) ICODE, QUAN, PRICE, AMOUNT, RTRIM(ISNULL(FCODE,'')) FCODE, "
                "RTRIM(ISNULL(COST_CENTER,'')) CC")
        start = '%s-01-01' % self.year
        end = '%s-01-01' % (self.year + 1)
        purchases = self._fetch(
            f"SELECT {cols} FROM din10 WHERE FTYPE='006' AND FDATE >= %s AND FDATE < %s "
            "ORDER BY FDATE, NUMBER1, SERIAL", (start, end))
        issues = self._fetch(
            f"SELECT {cols} FROM bin10 WHERE FTYPE='101' AND FDATE >= %s AND FDATE < %s "
            "ORDER BY FDATE, NUMBER1, SERIAL", (start, end))
        for r in purchases + issues:
            r['QUAN'] = float(r['QUAN'] or 0.0)
            r['AMOUNT'] = float(r['AMOUNT'] or 0.0)
            r['PRICE'] = float(r['PRICE'] or 0.0)
        return purchases, issues

    def _products(self):
        return {p.default_code.strip(): p for p in self.env['product.product'].search(
            [('default_code', '!=', False), ('is_storable', '=', True)])}

    def _warehouse_for(self, code2, whs):
        """Document branch → warehouse: 1 + warehouse code (115 → 15)."""
        code2 = (code2 or '').strip()
        return whs.get(code2[1:]) if code2[:1] == '1' else False

    # ------------------------------------------------------------------ 1. purchases
    def action_link_purchases_from_bas(self):
        self.ensure_one()
        purchases, _issues = self._bas_item_lines()
        products = self._products()
        by_doc = defaultdict(list)
        for r in purchases:
            by_doc[_key(r)].append(r)
        Move = self.env['account.move'].sudo()
        Review = self.env['ksw.bas.purchase.review'].sudo()
        stock_accounts = Review._stock_accounts()
        linked = relinked = same = 0
        problems = []
        for key, rows in by_doc.items():
            move = Move.search([('x_bas_key', '=', key), ('state', '=', 'posted')], limit=1)
            if not move:
                problems.append(f"{key}: no posted entry in Odoo")
                continue
            lines = move.line_ids.filtered(lambda l: l.account_id in stock_accounts and l.balance > 0)
            if len(lines) != 1:
                problems.append(f"{move.name} ({key}): {len(lines)} stock lines, expected 1")
                continue
            missing = [r['ICODE'] for r in rows if r['ICODE'] not in products]
            if missing:
                problems.append(f"{move.name}: items not in Odoo {missing}")
                continue
            line = lines
            review = Review.search([('line_id', '=', line.id)], limit=1) or Review.create({
                'line_id': line.id, 'pattern': False})
            wanted = sorted((products[r['ICODE']].id, r['QUAN']) for r in rows)
            have = sorted((p.product_id.id, p.quantity) for p in review.part_ids)
            if review.state == 'linked' and wanted == have:
                same += 1
                continue
            if review.state != 'review':
                review.action_reset()
                relinked += 1
            # BAS's amounts per item, scaled to the line so the parts add up
            # to it exactly (BAS's line total and the voucher can differ by VAT
            # rounding); the last part takes the residue.
            total = sum(r['AMOUNT'] for r in rows) or 1.0
            amounts = [round(review.amount * r['AMOUNT'] / total, 2) for r in rows]
            amounts[-1] = round(review.amount - sum(amounts[:-1]), 2)
            review.write({
                'part_ids': [fields.Command.clear()] + [fields.Command.create({
                    'product_id': products[r['ICODE']].id, 'quantity': r['QUAN'], 'amount': a})
                    for r, a in zip(rows, amounts)],
                'suggestion': self.env._('From BAS item lines (din10)'),
            })
            review.with_context(ksw_bas_exact=True).action_confirm()
            linked += 1
        out = [f"Purchases from BAS {self.year}: {len(by_doc)} documents; "
               f"{linked} linked, {same} already right, {relinked} corrected"]
        if problems:
            out.append("Not linked: " + "; ".join(problems))
        return self._done(out)

    # ------------------------------------------------------------------ 1b. entries → bills
    def action_convert_purchase_entries(self):
        """Turn BAS purchases (006) still sitting as journal entries into vendor bills.

        ``ksw.bas.gl.import.action_convert_bills`` does this for a date range,
        and was run up to 31 Aug 2026, so September's purchases stayed entries
        and a receipt could only open a journal entry. This converts **only the
        006 purchases**, with that same builder (same accounts, amounts, VAT,
        x_bas_key), and never one a payment is already matched to. The item
        lines are then relinked from BAS, since they hung on the entry's line.
        """
        self.ensure_one()
        company = self.company_id
        Move = self.env['account.move'].sudo().with_context(tracking_disable=True).with_company(company)
        entries = Move.search([
            ('move_type', '=', 'entry'), ('state', '=', 'posted'), ('company_id', '=', company.id),
            ('x_bas_key', '=like', '006/%'),
            ('date', '>=', date(self.year, 1, 1)), ('date', '<=', date(self.year, 12, 31))])
        matched = entries.filtered(lambda m: m.line_ids.filtered('reconciled'))
        # Oldest first, so the bills are numbered in date order.
        entries = (entries - matched).sorted(lambda m: (m.date, m.id))
        if not entries:
            return self._done([self.env._("No purchase entries to convert.")])
        numbers = sorted({int(k.split('/')[3]) for k in entries.mapped('x_bas_key')})
        rows = self._fetch(
            "SELECT FTYPE, FTYPE2, RTRIM(CODE2) CODE2, NUMBER1, SERIAL, RTRIM(ISNULL(FCODE,'')) FCODE, "
            "RTRIM(ISNULL(TCODE,'')) TCODE, AMOUNT, REF, FDATE FROM vou10 WHERE FTYPE='006' "
            f"AND NUMBER1 IN ({','.join(['%s'] * len(numbers))}) ORDER BY NUMBER1, SERIAL",
            tuple(numbers))
        vou = defaultdict(list)
        for r in rows:
            vou[f"{r['FTYPE']}/{r['FTYPE2']}/{r['CODE2']}/{r['NUMBER1']:.0f}"].append(r)
        gl = self.env['ksw.bas.gl.import'].sudo()
        t15, t0 = gl._purchase_taxes(company)
        accounts = {a.x_bas_code: a for a in self.env['account.account'].sudo().search(
            [('x_bas_code', '!=', False)])}
        partners = {p.x_bas_code: p for p in self.env['res.partner'].sudo().search(
            [('x_bas_code', '=like', '2101%')])}
        journal = gl._bill_journal(company)
        converted, problems = [], []
        for entry in entries:
            vals, why = gl._build_bill(entry.x_bas_key, vou.get(entry.x_bas_key, []), accounts,
                                       partners, t15, t0, journal, company)
            if vals is None:
                problems.append(f"{entry.name}: {why}")
                continue
            name = entry.name
            entry.button_draft()
            entry.with_context(force_delete=True).unlink()
            bill = Move.create(vals)
            bill._post(soft=False)
            converted.append(f"{name} → {bill.name}")
        if converted:
            # A rebuilt document must arrive with its BAS cost centres, like
            # every other conversion (ksw.bas.gl.import._apply_cost_centres_if_ready).
            gl._apply_cost_centres_if_ready(date(self.year, 1, 1), date(self.year, 12, 31), company)
        out = [self.env._("Purchase entries converted to vendor bills: %s", len(converted))] + converted
        if matched:
            out.append(self.env._("Left as entries, a payment is matched to them: %s",
                                  ", ".join(matched.mapped('name'))))
        if problems:
            out.append(self.env._("Not converted: ") + "; ".join(problems))
        link_log = self.action_link_purchases_from_bas() and self.log
        self.log = '\n'.join(out + [link_log])
        return self._done(self.log.split('\n'))

    # ------------------------------------------------------------------ 2. movements
    def action_rebuild_movement_history(self):
        self.ensure_one()
        company = self.company_id
        purchases, issues = self._bas_item_lines()
        products = self._products()
        whs = {w.x_bas_code: w for w in self.env['stock.warehouse'].search(
            [('x_bas_code', '!=', False), ('x_bas_kind', '=', 'warehouse'),
             ('company_id', '=', company.id)])}
        if not whs:
            raise UserError(self.env._("Run the setup first: no BAS warehouses yet."))

        # Which (item, warehouse) pairs add up in BAS.
        now = self._bas_quantities_now(set(r['ICODE'] for r in purchases + issues))
        flow = defaultdict(lambda: [0.0, 0.0])
        for r in purchases:
            flow[(r['ICODE'], r['CODE2'])][0] += r['QUAN']
        for r in issues:
            flow[(r['ICODE'], r['CODE2'])][1] += r['QUAN']
        refused, skipped_items = [], set()
        for (code, code2), (inq, outq) in flow.items():
            wh = self._warehouse_for(code2, whs)
            if code not in products or not wh:
                refused.append(f"{code}@{code2}: " + ("not a stock item" if code not in products else "no warehouse"))
                skipped_items.add(code)
                continue
            opening = now.get((code, wh.x_bas_code), 0.0) - inq + outq
            if opening < 0:
                refused.append(self.env._(
                    "%(item)s at %(wh)s: BAS shows %(now)s in stock but %(inq)s bought and %(outq)s "
                    "issued this year, so %(gap)s left without a document",
                    item=code, wh=wh.code, now=now.get((code, wh.x_bas_code), 0.0), inq=inq,
                    outq=outq, gap=-opening))
                skipped_items.add(code)

        todo = {code for code, _c in flow} - skipped_items
        # Already replayed: the opening balance is the item's marker.
        opening_name = _OPENING_NAME % self.year
        done_before = set(self.env['stock.move'].search([
            ('is_inventory', '=', True), ('inventory_name', '=', opening_name),
            ('product_id', 'in', [products[c].id for c in todo])]).mapped('product_id.default_code'))
        todo -= {c.strip() for c in done_before}
        if not todo:
            return self._done([self.env._("Nothing to rebuild.")] + (
                [self.env._("Not replayed: ") + "; ".join(refused)] if refused else []))

        start = datetime.combine(date(self.year, 1, 1), time(0, 0))
        prices = {c: products[c].with_company(company).standard_price for c in todo}
        categs = self.env['product.category'].browse(
            [self.env.ref(c).id for c, _a, _b in STOCK_CLASSES.values()]).with_company(company)
        analytic = self._vehicle_cost_centres({r['CC'] for r in issues if r['CC']})
        consumption = self.env.ref('KSW_inventory.location_consumption')
        bills = {m.x_bas_key: m for m in self.env['account.move'].sudo().search(
            [('x_bas_key', 'in', list({_key(r) for r in purchases}))])}
        Picking = self.env['stock.picking'].with_context(ksw_bas_replay=True, tracking_disable=True)
        existing = set(Picking.search([('x_bas_key', '!=', False)]).mapped('x_bas_key'))
        Account = self.env['account.account'].with_company(company)
        stats = defaultdict(int)

        def stock_now():
            return {(c, w.code): products[c].with_context(location=w.lot_stock_id.id).qty_available
                    for c in todo for w in whs.values()}
        before = stock_now()

        categs.write({'property_valuation': 'periodic'})
        try:
            # a) the opening load becomes the opening, dated 1 January
            todo_products = self.env['product.product'].browse([products[c].id for c in todo])
            old = self.env['stock.move'].search([
                ('product_id', 'in', todo_products.ids), ('state', '=', 'done'),
                ('is_inventory', '=', True), ('date', '>=', start)])
            old.write({'date': start})
            old.move_line_ids.write({'date': start})
            stats['opening lines re-dated'] = len(old)

            # b) the opening balance: issues − purchases, so today's stock stays put
            vals = []
            for (code, code2), (inq, outq) in flow.items():
                if code not in todo:
                    continue
                delta = outq - inq
                if not delta:
                    continue
                wh = self._warehouse_for(code2, whs)
                product = products[code]
                inv_loc = product.with_company(company).property_stock_inventory
                src, dst = (inv_loc, wh.lot_stock_id) if delta > 0 else (wh.lot_stock_id, inv_loc)
                vals.append({
                    'product_id': product.id, 'product_uom': product.uom_id.id,
                    'product_uom_qty': abs(delta), 'quantity': abs(delta), 'picked': True,
                    'location_id': src.id, 'location_dest_id': dst.id, 'company_id': company.id,
                    'is_inventory': True, 'inventory_name': opening_name,
                })
            opening_moves = self.env['stock.move'].with_context(inventory_mode=False).create(vals)
            opening_moves._action_done()
            opening_moves.write({'date': start})
            opening_moves.move_line_ids.write({'date': start})
            stats['opening balances'] = len(opening_moves)

            # c) one receipt per purchase, one issue per issue, in date order
            docs = defaultdict(list)
            for r in purchases:
                docs[('in', _key(r))].append(r)
            for r in issues:
                docs[('out', _key(r))].append(r)
            for (direction, key), rows in sorted(docs.items(), key=lambda kv: kv[1][0]['FDATE']):
                rows = [r for r in rows if r['ICODE'] in todo]
                if not rows or key in existing:
                    continue
                wh = self._warehouse_for(rows[0]['CODE2'], whs)
                when = rows[0]['FDATE']
                if direction == 'in':
                    bill = bills.get(key)
                    ptype, src, dst = wh.in_type_id, self.env.ref('stock.stock_location_suppliers'), wh.lot_stock_id
                    vendor = (bill.partner_id or self.env['ksw.bas.purchase.review']._vendor_of_move(bill)) \
                        if bill else False
                    extra = {'partner_id': vendor.id if vendor else False,
                             'origin': bill.name if bill else key}
                else:
                    ptype = self.env['stock.picking.type'].search(
                        [('warehouse_id', '=', wh.id), ('x_ksw_issue', '=', True)], limit=1)
                    src, dst = wh.lot_stock_id, consumption
                    acc = Account.search([('code', '=', rows[0]['FCODE'])], limit=1) if rows[0]['FCODE'] else False
                    extra = {'origin': ' / '.join(filter(None, [key, rows[0]['CC']])),
                             'x_issue_account_id': acc.id if acc else False}
                picking = Picking.create(dict(extra, **{
                    'picking_type_id': ptype.id, 'location_id': src.id, 'location_dest_id': dst.id,
                    'company_id': company.id, 'x_bas_key': key, 'scheduled_date': when,
                    'move_ids': [fields.Command.create({
                        'product_id': products[r['ICODE']].id,
                        'product_uom': products[r['ICODE']].uom_id.id,
                        'product_uom_qty': r['QUAN'], 'quantity': r['QUAN'], 'picked': True,
                        'location_id': src.id, 'location_dest_id': dst.id,
                        'price_unit': r['PRICE'],
                        'description_picking': r['CC'] or False,
                        'analytic_distribution': ({str(analytic[_norm(r['CC'])]): 100}
                                                  if direction == 'out' and _norm(r['CC']) in analytic
                                                  else False),
                    }) for r in rows],
                }))
                picking.action_confirm()
                picking.move_ids.write({'quantity': 0})
                for move, r in zip(picking.move_ids, rows):
                    move.quantity = r['QUAN']
                    move.picked = True
                picking._action_done()
                picking.write({'date_done': when, 'scheduled_date': when})
                picking.move_ids.write({'date': when})
                picking.move_ids.move_line_ids.write({'date': when})
                stats['receipts' if direction == 'in' else 'issues'] += 1
        finally:
            categs.write({'property_valuation': 'real_time'})
            for code, price in prices.items():
                products[code].with_company(company).standard_price = price

        # Today's stock must be what it was: the replay adds history, not stock.
        self.env.invalidate_all()
        after = stock_now()
        off = [f"{c}@{w}: {before[(c, w)]} → {after[(c, w)]}" for (c, w) in before
               if abs(before[(c, w)] - after[(c, w)]) > 0.001]
        if off:
            raise UserError(self.env._(
                "The rebuilt history would change today's stock, so nothing was saved: %s",
                "; ".join(off)))
        posted = self.env['stock.move'].search_count([
            ('product_id', 'in', todo_products.ids), ('account_move_id', '!=', False)])
        if posted:
            raise UserError(self.env._(
                "The rebuild would have posted %s journal entries, so nothing was saved.", posted))
        out = [self.env._("Movement history %(year)s rebuilt for %(n)s items: %(stats)s. "
                          "Stock today unchanged; nothing posted.",
                          year=self.year, n=len(todo),
                          stats=", ".join(f"{v} {k}" for k, v in stats.items()))]
        if refused:
            out.append(self.env._("Not replayed (BAS's figures do not add up): ") + "; ".join(refused))
        return self._done(out)

    def _bas_quantities_now(self, codes):
        """{(item, warehouse code): BAS quantity today} from ITMW10's per-warehouse columns."""
        if not codes:
            return {}
        cols = [r['c'] for r in self._fetch(
            "SELECT COLUMN_NAME c FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME='ITMW10' AND COLUMN_NAME LIKE 'IAQ1%'")]
        codes = sorted(codes)
        rows = self._fetch(
            f"SELECT RTRIM(ICODE) code, {', '.join(cols)} FROM ITMW10 "
            f"WHERE RTRIM(ICODE) IN ({','.join(['%s'] * len(codes))})", tuple(codes))
        return {(r['code'], c[4:]): float(r[c]) for r in rows for c in cols if r[c]}

    def _vehicle_cost_centres(self, names):
        """{normalised BAS cost-centre name: analytic account id}, via WREF10 codes."""
        if not names:
            return {}
        wanted = {_norm(n) for n in names}
        codes = {}
        for r in self._fetch("SELECT RTRIM(CODE) code, NAME FROM WREF10 ORDER BY CODE"):
            key = _norm(r['NAME'])
            if key in wanted:
                codes.setdefault(key, r['code'])
        accounts = {a.x_bas_code: a.id for a in self.env['account.analytic.account'].sudo().search(
            [('x_bas_code', 'in', list(codes.values())), ('root_plan_id.x_bas_code', '=', 'WREF10')])}
        return {name: accounts[code] for name, code in codes.items() if code in accounts}
