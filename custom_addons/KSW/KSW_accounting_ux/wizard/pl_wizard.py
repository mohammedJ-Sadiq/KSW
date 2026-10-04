from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.exceptions import UserError


class KswPlWizard(models.TransientModel):
    """Ask for a date range, then open the Income Statement for exactly it.

    Builds a *temporary* MIS instance (mis_builder vacuums those after a day)
    with fixed-date columns, so every user gets their own range without
    touching the saved instances.
    """

    _name = 'ksw.pl.wizard'
    _description = 'Profit and Loss: choose period'

    date_from = fields.Date(
        'From', required=True,
        default=lambda self: fields.Date.context_today(self).replace(month=1, day=1))
    date_to = fields.Date(
        'To', required=True, default=lambda self: fields.Date.context_today(self))
    layout = fields.Selection([
        ('summary', 'Summary'),
        ('account', 'By account'),
        ('monthly', 'Month by month'),
    ], required=True, default='summary')

    @api.constrains('date_from', 'date_to')
    def _check_dates(self):
        for wiz in self:
            if wiz.date_from > wiz.date_to:
                raise UserError('"From" must be on or before "To".')

    def _periods(self):
        self.ensure_one()
        fmt = lambda d: d.strftime('%d/%m/%Y')
        whole = {'name': f'{fmt(self.date_from)} - {fmt(self.date_to)}',
                 'mode': 'fix', 'manual_date_from': self.date_from,
                 'manual_date_to': self.date_to}
        if self.layout != 'monthly':
            return [whole]
        cols, start = [], self.date_from
        while start <= self.date_to:
            month_end = date(start.year, start.month, 1) + relativedelta(months=1, days=-1)
            end = min(month_end, self.date_to)
            cols.append({'name': start.strftime('%m/%Y'), 'mode': 'fix',
                         'manual_date_from': start, 'manual_date_to': end})
            start = end + relativedelta(days=1)
        if len(cols) > 1:
            cols.append(dict(whole, name='الإجمالي'))
        return cols

    def action_open(self):
        self.ensure_one()
        report = self.env.ref('KSW_accounting_ux.mis_pl_report')
        periods = [(0, 0, dict(p, sequence=i * 10)) for i, p in enumerate(self._periods())]
        # sudo: creating an instance is a manager right in mis_builder, but
        # this one is a throw-away view built only from the values above; the
        # figures are still computed as the user who opens it.
        instance = self.env['mis.report.instance'].sudo().create({
            'name': f'Profit and Loss {self.date_from} - {self.date_to}',
            'report_id': report.id,
            'temporary': True,
            'company_id': self.env.company.id,
            'target_move': 'posted',
            'display_columns_description': True,
            'no_auto_expand_accounts': self.layout != 'account',
            'landscape_pdf': self.layout == 'monthly',
            'period_ids': periods,
        })
        return {
            'type': 'ir.actions.act_window',
            'name': 'Profit and Loss',
            'res_model': 'mis.report.instance',
            'res_id': instance.id,
            'view_mode': 'form',
            'views': [(self.env.ref('mis_builder.mis_report_instance_result_view_form').id, 'form')],
            'target': 'current',
            # mis_report_widget reads its instance from active_id (Pitfalls #183)
            'context': {'active_model': 'mis.report.instance', 'active_id': instance.id},
        }
