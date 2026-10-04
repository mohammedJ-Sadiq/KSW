import logging
from collections import defaultdict
from datetime import date, timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

ONE_DAY = timedelta(days=1)


class KswBasFixedAsset(models.Model):
    """One BAS fixed asset for one fiscal year, plus what BAS needs to charge
    its depreciation for ANY date range the way BAS's own report does.

    BAS never journalises depreciation during the year (its trial balance
    shows none); its income statement / "depreciation of assets" report
    computes it for the range asked.  Reverse-engineered against BAS's own
    figures (2026: groups 1902-1906 for Jan-Jun, 1903-1905 for Jul-Aug,
    190201 for Jan-Sep, total group 33 for Apr-Jun, and twelve 1904 assets
    line by line, every one to the halala), the rule for a range [a, b] is:

    * accumulated at ``a`` = opening accumulated + the charge for
      [1 Jan, a - 1] computed by this same rule
    * cost at ``a`` = REGISTER cost (FIXVOU10 type 03, not the account
      balance) + every addition dated before ``a``, merged in
    * charge = min(cost x rate / 365 x days, cost - 1 - accumulated at a),
      i.e. stops at a book value of 1 SAR
    * + each addition dated inside the range, on its own:
      min(amount x rate / 365 x days from its date, amount - 1)
    * a disposal before ``a``: 0; inside the range: |DEP_AMOUNT -
      accumulated at a|, DEP_AMOUNT being what BAS's disposal entry records

    Because the result depends on where the range STARTS, it cannot be a
    sum of stored daily amounts: the income statement calls
    ``_bas_charge`` per column (see KSW_accounting_ux mis_report override).
    """

    _name = 'ksw.bas.fixed.asset'
    _description = 'BAS Fixed Asset (depreciation basis)'
    _order = 'year desc, code'

    code = fields.Char(required=True, index=True)
    year = fields.Integer(required=True, index=True)
    # 1 January of ``year``; the date field the MIS query filters on.
    date = fields.Date(required=True)
    asset_account_id = fields.Many2one('account.account', required=True)
    expense_account_id = fields.Many2one('account.account', required=True)
    expense_group = fields.Char(required=True, index=True)   # 3302 ...
    rate = fields.Float('Annual rate %', required=True)
    cost = fields.Float('Register cost at 1 Jan', required=True)
    accum_open = fields.Float('Accumulated at 1 Jan', required=True)
    # Always 0 in the table: the MIS query sums it, then the real amount for
    # the column's range replaces it (KSW_accounting_ux).
    amount = fields.Float()
    event_ids = fields.One2many('ksw.bas.fixed.asset.event', 'asset_id')
    company_id = fields.Many2one('res.company', required=True, index=True)

    # ------------------------------------------------------------------
    def _bas_period(self, a, b, acc_a, events):
        self.ensure_one()
        rate = self.rate / 100.0 / 365.0
        cost, adds_in, disposal = self.cost, [], None
        for e in events:
            if e.kind == 'add':
                if e.date < a:
                    cost += e.amount
                elif e.date <= b:
                    adds_in.append(e)
            else:
                disposal = e
        if disposal and disposal.date < a:
            return 0.0
        if disposal and disposal.date <= b:
            # BAS shows the SIZE of the gap between what its disposal entry
            # records and the accumulated at ``a``: 1906010073, Q2 2026 =
            # |43,834.91 - 47,872.93| = 4,038.02 on BAS's own statement.
            return abs(disposal.dep_amount - acc_a)
        charge = max(0.0, min(cost * rate * ((b - a).days + 1), cost - 1.0 - acc_a))
        for e in adds_in:
            charge += max(0.0, min(e.amount * rate * ((b - e.date).days + 1), e.amount - 1.0))
        return charge

    def _bas_charge(self, date_from, date_to):
        """BAS depreciation for [date_from, date_to], summed over ``self``.

        Only the part of the range inside each asset's ``year`` counts.
        """
        total = 0.0
        events_by_asset = defaultdict(list)
        for e in self.event_ids:
            events_by_asset[e.asset_id.id].append(e)
        for asset in self:
            ys, ye = date(asset.year, 1, 1), date(asset.year, 12, 31)
            a, b = max(date_from, ys), min(date_to, ye)
            if a > b:
                continue
            ev = events_by_asset[asset.id]
            acc_a = asset.accum_open
            if a > ys:
                acc_a += asset._bas_period(ys, a - ONE_DAY, asset.accum_open, ev)
            total += asset._bas_period(a, b, acc_a, ev)
        return total

    # ------------------------------------------------------------------
    @api.model
    def _rebuild_year(self, year=None, company=None):
        """Reload ``year``'s asset basis from BAS (its live DB's open year only)."""
        company = company or self.env.company
        year = year or fields.Date.context_today(self).year
        Imp = self.env['ksw.bas.gl.import']
        conn = Imp._bas_connect()
        cur = conn.cursor(as_dict=True)
        cur.execute("SELECT YEAR(MIN(FDATE)) y FROM vou10")
        bas_year = cur.fetchone()['y']
        if bas_year != year:
            conn.close()
            raise UserError(f'BAS is open on {bas_year}; cannot load {year}.')
        cur.execute("""
            SELECT RTRIM(a.DCODE1) code, a.DDEP_PER per, RTRIM(a.DDEP_CODE) exp,
                   a.DOLDACC cost_open, ISNULL(d.DOLDACC, 0) accum_open
            FROM COD10 a
            LEFT JOIN COD10 d ON RTRIM(d.DCODE1) = RTRIM(a.ACCUMULATECODE)
            WHERE a.DLEVEL = 5 AND a.DCODE1 LIKE '19%%'
              AND a.DDEP_PER > 0 AND RTRIM(ISNULL(a.DDEP_CODE, '')) <> ''
        """)
        assets = cur.fetchall()
        # Register cost, not the account balance: 1905010011 carries
        # 1,532,265.87 on its account but 1,147,265.87 in the register.
        cur.execute("""
            SELECT RTRIM(DCODE1) code, SUM(FIX_AMOUNT) cost
            FROM FIXVOU10 WHERE FIX_TYPE = '03' GROUP BY RTRIM(DCODE1)
        """)
        register_cost = {r['code']: float(r['cost'] or 0.0) for r in cur.fetchall()}
        cur.execute("""
            SELECT RTRIM(DCODE1) code, FIX_TYPE, FDATE, FIX_AMOUNT, DEP_AMOUNT
            FROM FIXVOU10
            WHERE FIX_TYPE IN ('01', '02') AND FDATE >= %s AND FDATE < %s
        """, (date(year, 1, 1), date(year + 1, 1, 1)))
        events = defaultdict(list)
        for e in cur.fetchall():
            events[e['code']].append((0, 0, {
                'date': e['FDATE'].date(),
                'kind': 'add' if e['FIX_TYPE'] == '01' else 'dispose',
                'amount': float(e['FIX_AMOUNT'] or 0.0),
                'dep_amount': float(e['DEP_AMOUNT'] or 0.0),
            }))
        conn.close()

        accounts = {a.x_bas_code: a.id for a in self.env['account.account'].search(
            [('x_bas_code', '!=', False)])}
        vals, missing = [], []
        for a in assets:
            asset_id, exp_id = accounts.get(a['code']), accounts.get(a['exp'])
            if not asset_id or not exp_id:
                missing.append(a['code'] if not asset_id else a['exp'])
                continue
            vals.append({
                'code': a['code'], 'year': year, 'date': date(year, 1, 1),
                'asset_account_id': asset_id, 'expense_account_id': exp_id,
                'expense_group': a['exp'][:4], 'rate': float(a['per']),
                'cost': register_cost.get(a['code'], -float(a['cost_open'] or 0.0)),
                'accum_open': float(a['accum_open'] or 0.0),
                'event_ids': events.get(a['code'], []),
                'company_id': company.id,
            })
        self.search([('year', '=', year), ('company_id', '=', company.id)]).unlink()
        self.create(vals)
        if missing:
            _logger.warning('BAS fixed assets %s: no Odoo account for %s', year, sorted(missing)[:20])
        _logger.info('BAS fixed assets %s: %s assets loaded', year, len(vals))
        return len(vals)

    @api.model
    def _cron_rebuild(self):
        self._rebuild_year()


class KswBasFixedAssetEvent(models.Model):
    _name = 'ksw.bas.fixed.asset.event'
    _description = 'BAS Fixed Asset Addition / Disposal'
    _order = 'date'

    asset_id = fields.Many2one('ksw.bas.fixed.asset', required=True, ondelete='cascade', index=True)
    date = fields.Date(required=True)
    kind = fields.Selection([('add', 'Addition'), ('dispose', 'Disposal')], required=True)
    amount = fields.Float(required=True)
    dep_amount = fields.Float('Accumulated at disposal')
