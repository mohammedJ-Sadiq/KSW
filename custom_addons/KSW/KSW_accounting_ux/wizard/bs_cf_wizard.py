from dateutil.relativedelta import relativedelta

from odoo import fields, models


class KswCfWizard(models.TransientModel):
    """Ask for a date range, then open the Cash Flow Statement for it."""

    _name = 'ksw.cf.wizard'
    _inherit = 'ksw.mis.period.wizard'
    _description = 'Cash Flow Statement: choose period'

    # Movements by account mean nothing on a cash flow: no "By account".
    layout = fields.Selection([
        ('summary', 'Summary'),
        ('monthly', 'Month by month'),
    ], required=True, default='summary')

    def action_open(self):
        return self._open_statement('KSW_accounting_ux.mis_cf_report', self.env._('Cash Flow Statement'))


class KswBsWizard(models.TransientModel):
    """Ask for a date, then open the Balance Sheet as of it.

    Each column runs from 1 January of its year to its date: balance-sheet
    accounts ignore the start (``bale`` sums from the beginning of time),
    while the current-year result and BAS's computed depreciation need it.
    """

    _name = 'ksw.bs.wizard'
    _inherit = 'ksw.mis.wizard.mixin'
    _description = 'Balance Sheet: choose date'

    date_to = fields.Date(
        'As of', required=True, default=lambda self: fields.Date.context_today(self))
    comparison = fields.Selection([
        ('none', 'No comparison'),
        ('prev_month', 'End of previous month'),
        ('prev_year', 'End of previous year'),
    ], required=True, default='prev_year')
    layout = fields.Selection([
        ('summary', 'Summary'),
        ('account', 'By account'),
    ], required=True, default='summary')

    def _column(self, as_of):
        start = self.env.company.compute_fiscalyear_dates(as_of)['date_from']
        return {'name': as_of.strftime('%d/%m/%Y'), 'mode': 'fix',
                'manual_date_from': start, 'manual_date_to': as_of}

    def _periods(self):
        self.ensure_one()
        cols = [self._column(self.date_to)]
        if self.comparison == 'prev_month':
            cols.append(self._column(self.date_to.replace(day=1) - relativedelta(days=1)))
        elif self.comparison == 'prev_year':
            start = self.env.company.compute_fiscalyear_dates(self.date_to)['date_from']
            cols.append(self._column(start - relativedelta(days=1)))
        return cols

    def action_open(self):
        self.ensure_one()
        return self._open_mis_instance(
            'KSW_accounting_ux.mis_bs_report', f"{self.env._('Balance Sheet')} {self.date_to}",
            self._periods(), by_account=self.layout == 'account')
