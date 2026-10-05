import re

from odoo import fields, models
from odoo.fields import Domain
from odoo.tools.safe_eval import safe_eval

from odoo.addons.mis_builder.models.mis_report import AutoStruct


class MisReport(models.Model):
    _inherit = 'mis.report'

    def _fetch_queries(self, date_from, date_to, get_additional_query_filter=None):
        """BAS depreciation: compute each column's own range.

        A query on ``ksw.bas.fixed.asset`` only selects assets (its domain
        names the expense group). BAS's charge for a range depends on where
        the range starts, so it cannot be summed from stored rows: replace
        the stock result with ``_bas_charge(date_from, date_to)``.
        """
        res = super()._fetch_queries(date_from, date_to, get_additional_query_filter)
        for query in self.query_ids.filtered(
                lambda q: q.sudo().model_id.model == 'ksw.bas.fixed.asset'):
            assets = self._ksw_bas_assets(query, get_additional_query_filter)
            res[query.name] = AutoStruct(
                count=len(assets), amount=assets._bas_charge(date_from, date_to))
        return res

    def _ksw_bas_assets(self, query, get_additional_query_filter=None):
        """The assets a ``ksw.bas.fixed.asset`` query selects."""
        domain = safe_eval(query.domain) if query.domain else []
        if get_additional_query_filter:
            domain += get_additional_query_filter(query)
        return self.env['ksw.bas.fixed.asset'].search(
            domain + [('company_id', 'in', self.env.companies.ids)])


class MisReportInstance(models.Model):
    _inherit = 'mis.report.instance'

    def drilldown(self, arg):
        """Open journal items one chart level below the clicked line.

        Stock mis_builder opens a flat list of every journal item; BAS drills
        level by level (3302 -> 330201 -> account -> entries), so group the
        list the same way. Applies to every MIS report.
        """
        action = self._ksw_drilldown(arg)
        if action and action.get('res_model') == 'account.move.line' and action.get('domain'):
            accounts = self.env['account.move.line']._read_group(
                Domain(action['domain']), ['account_id'])
            group_by = self.env['account.account'].union(
                *(a for a, in accounts))._ksw_level_group_by()
            if group_by:
                action['context'] = dict(action.get('context') or {}, group_by=group_by)
        return action

    def _ksw_drilldown(self, arg):
        """Depreciation: list the expense sub-accounts that make up the cell.

        The stock drilldown only opens journal items, and BAS depreciation has
        none (it is computed per range, see ``_fetch_queries``), so clicking a
        depreciation figure showed an empty list.
        """
        self.ensure_one()
        expr = arg.get('expr') or ''
        names = set(re.findall(r'\b(\w+)\.amount\b', expr))
        queries = self.report_id.query_ids.filtered(
            lambda q: q.name in names and q.sudo().model_id.model == 'ksw.bas.fixed.asset')
        period = self.env['mis.report.instance.period'].browse(arg.get('period_id'))
        if not queries or not period:
            return super().drilldown(arg)
        account_id = arg.get('account_id')
        rows = []
        for query in queries:
            assets = self.report_id._ksw_bas_assets(query, period._get_additional_query_filter)
            for asset in assets:
                if account_id and asset.expense_account_id.id != account_id:
                    continue
                amount = asset._bas_charge(period.date_from, period.date_to)
                if amount:
                    rows.append({
                        'source': 'bas', 'asset_id': asset.id,
                        'account_id': asset.expense_account_id.id,
                        'asset_account_id': asset.asset_account_id.id,
                        'expense_group': asset.expense_group,
                        'rate': asset.rate, 'amount': amount,
                    })
        # The balp[33xx] half of the expression: manual journal entries, if any.
        stock = super().drilldown(arg)
        if stock and stock.get('domain'):
            groups = self.env['account.move.line']._read_group(
                Domain(stock['domain']), ['account_id'], ['balance:sum'])
            for account, balance in groups:
                if balance:
                    rows.append({
                        'source': 'journal', 'account_id': account.id,
                        'expense_group': (account.code or '')[:4], 'amount': balance,
                    })
        lines = self.env['ksw.pl.depreciation.line'].create(rows)
        return {
            'name': self._get_drilldown_action_name(arg),
            'type': 'ir.actions.act_window',
            'res_model': 'ksw.pl.depreciation.line',
            'views': [[False, 'list'], [False, 'pivot']],
            'view_mode': 'list,pivot',
            'domain': [('id', 'in', lines.ids)],
            'target': 'current',
            'context': {'group_by': lines.account_id._ksw_level_group_by()},
        }


class KswPlDepreciationLine(models.TransientModel):
    """One expense sub-account's share of a depreciation figure on the P&L."""

    _name = 'ksw.pl.depreciation.line'
    _description = 'Profit and Loss: depreciation detail'
    _order = 'account_id'

    source = fields.Selection(
        [('bas', 'BAS (computed)'), ('journal', 'Journal entry')], required=True)
    account_id = fields.Many2one('account.account', 'Expense Account', required=True)
    asset_id = fields.Many2one('ksw.bas.fixed.asset', 'Asset')
    asset_account_id = fields.Many2one('account.account', 'Asset Account')
    expense_group = fields.Char('Group')
    x_group_l1_id = fields.Many2one(related='account_id.x_group_l1_id', store=True)
    x_group_l2_id = fields.Many2one(related='account_id.x_group_l2_id', store=True)
    x_group_l3_id = fields.Many2one(related='account_id.x_group_l3_id', store=True)
    x_group_l4_id = fields.Many2one(related='account_id.x_group_l4_id', store=True)
    rate = fields.Float('Rate %')
    amount = fields.Float()
