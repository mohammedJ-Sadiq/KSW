"""Vehicle, driver and salesperson on the BAS sales in the ledger.

Where BAS keeps them:

* ``vou10.COST_CENTER`` / ``COST_CENTER2`` on each **delivery note** (600):
  the vehicle and the driver. The month-end invoice (002) that bills the notes
  carries only the branch, so ``action_apply_cost_centres`` (which reads the
  invoice's own voucher) can only ever give an invoice line the branch.
* ``vou10.SELLER`` on each document: the BAS rep code, mapped to a person in
  BAS Sales Reps (``ksw.bas.sales.rep``, KSW_commissions).

``action_rebuild_invoice_lines`` already turns an invoice into one line per
delivery note (``"WH/OUT/303530 · 2026-07-01"``). ``action_tag_sales_dimensions``
gives each such line its own note's vehicle and driver, and the invoice its
salesperson. It follows BAS's own convention for the Cost Centres column
(COST_CENTER before COST_CENTER3), so a line that has its vehicle shows the
vehicle there rather than the branch; the branch stays on the invoice
(``x_bas_branch``). A rep that is not a person (house account, branch
placeholder, or not yet mapped) leaves the salesperson empty: OdooBot, the
importing user, is never a salesperson.
"""
import logging
import re
from collections import defaultdict

from odoo import api, fields, models

_logger = logging.getLogger(__name__)
_CHUNK = 900


def _norm(name):
    return re.sub(r'\s+', ' ', (name or '').replace('\xa0', ' ')).strip().lower()


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    x_salesperson_id = fields.Many2one(
        'res.users', string='Salesperson', related='move_id.invoice_user_id', store=True,
        index=True)


class BasGlImport(models.Model):
    _inherit = 'ksw.bas.gl.import'

    def action_rebuild_invoice_lines(self, date_from, date_to, company=None, bas_keys=None):
        res = super().action_rebuild_invoice_lines(date_from, date_to, company=company,
                                                   bas_keys=bas_keys)
        # The rebuilt lines are new: give them their vehicle and driver again.
        self.action_tag_sales_dimensions(date_from, date_to, company=company)
        return res

    # ------------------------------------------------------------------
    def _bas_rows_by_doc(self, cur, keys, select):
        """{doc key: [rows]} from vou10 for ``FTYPE/FTYPE2/CODE2/NUMBER1`` keys."""
        groups = defaultdict(set)
        for key in keys:
            ftype, ftype2, code2, number = key.split('/')
            groups[(ftype, int(ftype2), code2)].add(int(number))
        out = defaultdict(list)
        for (ftype, ftype2, code2), numbers in groups.items():
            numbers = sorted(numbers)
            for i in range(0, len(numbers), _CHUNK):
                part = numbers[i:i + _CHUNK]
                cur.execute(
                    f"SELECT CAST(NUMBER1 AS bigint) N, {select} FROM vou10 "
                    "WHERE FTYPE = %s AND FTYPE2 = %s AND RTRIM(CODE2) = %s "
                    f"AND CAST(NUMBER1 AS bigint) IN ({','.join(['%s'] * len(part))})",
                    (ftype, ftype2, code2, *part))
                for r in cur.fetchall():
                    out[f"{ftype}/{ftype2}/{code2}/{r['N']}"].append(r)
        return out

    def _rep_users(self):
        """{BAS rep code: res.users} for reps mapped to a person with a user."""
        reps = self.env['ksw.bas.sales.rep'].sudo().search([('employee_id', '!=', False),
                                                            ('is_house', '=', False)])
        return {r.code: r.employee_id.user_id for r in reps if r.employee_id.user_id}

    @api.model
    def action_tag_sales_dimensions(self, date_from=None, date_to=None, company=None, commit=True):
        company = company or self.env.company
        Move = self.env['account.move'].sudo().with_context(tracking_disable=True)
        domain = [('move_type', 'in', ('out_invoice', 'out_refund')), ('state', '=', 'posted'),
                  ('company_id', '=', company.id), ('x_bas_key', '!=', False)]
        if date_from:
            domain.append(('date', '>=', date_from))
        if date_to:
            domain.append(('date', '<=', date_to))
        invoices = Move.search(domain, order='date, id')
        if not invoices:
            return {'invoices': 0}
        stats = defaultdict(int)
        unmapped = defaultdict(int)
        rep_users = self._rep_users()

        cc_root, emp_root = self._cost_centre_root_plan(), self._employee_root_plan()
        Account = self.env['account.analytic.account'].sudo().with_context(active_test=False)
        cc_acc = {a.x_bas_code: a.id for a in Account.search(
            [('x_bas_code', '!=', False),
             ('root_plan_id', 'in', (cc_root | self._cost_centre_root_plans()).ids)])}
        emp_acc = {a.x_bas_code: a.id for a in Account.search(
            [('x_bas_code', '!=', False), ('root_plan_id', '=', emp_root.id)])}

        conn = self._bas_connect()
        try:
            cur = conn.cursor(as_dict=True)
            cur.execute("SELECT RTRIM(CODE) code, NAME FROM WREF10 ORDER BY CODE")
            by_name, code_name = {}, {}
            for r in cur.fetchall():
                code_name[r['code']] = (r['NAME'] or '').strip() or r['code']
                by_name.setdefault(_norm(r['NAME']), r['code'])

            def emp_account(code):
                if code not in emp_acc:
                    emp_acc[code] = Account.create({
                        'name': code_name[code], 'code': code, 'x_bas_code': code,
                        'plan_id': emp_root.id, 'company_id': company.id}).id
                return emp_acc[code]

            for i in range(0, len(invoices), 200):
                chunk = invoices[i:i + 200]
                # 1. Salesperson from the document's seller.
                sellers = self._bas_rows_by_doc(cur, chunk.mapped('x_bas_key'),
                                                "RTRIM(ISNULL(SELLER, '')) seller")
                by_user = defaultdict(list)
                for inv in chunk:
                    codes = {r['seller'] for r in sellers.get(inv.x_bas_key, []) if r['seller']}
                    code = min(codes) if codes else ''
                    user = rep_users.get(code, self.env['res.users'])
                    if not user and code:
                        unmapped[code] += 1
                    if inv.invoice_user_id != user:
                        by_user[user.id or False].append(inv.id)
                for uid, ids in by_user.items():
                    Move.browse(ids).write({'invoice_user_id': uid})
                    stats['salesperson set' if uid else 'salesperson cleared'] += len(ids)

                # 2. Vehicle and driver from each line's own delivery note.
                lines = chunk.invoice_line_ids.filtered(
                    lambda l: l.display_type == 'product' and ' · ' in (l.name or ''))
                if not lines:
                    continue
                names = {l.name.split(' · ', 1)[0] for l in lines}
                pickings = {p.name: p.x_bas_key for p in self.env['stock.picking'].sudo().search(
                    [('name', 'in', list(names)), ('x_bas_key', '=like', '600/%')])}
                notes = self._bas_rows_by_doc(
                    cur, set(pickings.values()),
                    "RTRIM(ISNULL(COST_CENTER, '')) cc1, RTRIM(ISNULL(COST_CENTER2, '')) cc2")
                assign = defaultdict(list)
                for line in lines:
                    key = pickings.get(line.name.split(' · ', 1)[0])
                    rows = [r for r in notes.get(key, []) if r['cc1'] or r['cc2']]
                    if not rows:
                        stats['lines without a BAS note'] += 1
                        continue
                    r = rows[0]
                    ids = []
                    veh = by_name.get(_norm(r['cc1']))
                    if veh in cc_acc:
                        ids.append(str(cc_acc[veh]))
                    drv = by_name.get(_norm(r['cc2']))
                    if drv and _norm(r['cc2']) != _norm(r['cc1']):
                        ids.append(str(emp_account(drv)))
                    if not ids:
                        stats['lines with unknown cost centres'] += 1
                        continue
                    dist = {','.join(ids): 100.0}
                    if line.analytic_distribution != dist:
                        assign[','.join(ids)].append(line.id)
                for key, ids in assign.items():
                    self.env['account.move.line'].sudo().browse(ids).write(
                        {'analytic_distribution': {key: 100.0}})
                    stats['lines tagged'] += len(ids)
                if commit:
                    self.env.cr.commit()
        finally:
            conn.close()
        res = {'invoices': len(invoices), **stats,
               'unmapped reps': dict(sorted(unmapped.items(), key=lambda kv: -kv[1]))}
        _logger.info('BAS sales dimensions %s..%s: %s', date_from, date_to, res)
        return res
