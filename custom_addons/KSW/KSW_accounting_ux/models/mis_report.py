from odoo import models
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
            domain = safe_eval(query.domain) if query.domain else []
            if get_additional_query_filter:
                domain += get_additional_query_filter(query)
            assets = self.env['ksw.bas.fixed.asset'].search(
                domain + [('company_id', 'in', self.env.companies.ids)])
            res[query.name] = AutoStruct(
                count=len(assets), amount=assets._bas_charge(date_from, date_to))
        return res
