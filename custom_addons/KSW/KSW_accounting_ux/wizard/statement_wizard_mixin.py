from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.exceptions import UserError


class KswMisWizardMixin(models.AbstractModel):
    """Open one of the KSW MIS statements for columns the user chose.

    Builds a *temporary* MIS instance (mis_builder vacuums those after a day)
    with fixed-date columns, so every user gets their own range without
    touching the saved instances.
    """

    _name = 'ksw.mis.wizard.mixin'
    _description = 'Financial statement: open for chosen columns'

    def _open_mis_instance(self, report_xmlid, title, periods,
                           by_account=False, landscape=False):
        report = self.env.ref(report_xmlid)
        periods = [(0, 0, dict(p, sequence=i * 10)) for i, p in enumerate(periods)]
        # sudo: creating an instance is a manager right in mis_builder, but
        # this one is a throw-away view built only from the values above; the
        # figures are still computed as the user who opens it.
        instance = self.env['mis.report.instance'].sudo().create({
            'name': title,
            'report_id': report.id,
            'temporary': True,
            'company_id': self.env.company.id,
            'target_move': 'posted',
            'display_columns_description': True,
            'no_auto_expand_accounts': not by_account,
            'landscape_pdf': landscape,
            'period_ids': periods,
        })
        return {
            'type': 'ir.actions.act_window',
            'name': title,
            'res_model': 'mis.report.instance',
            'res_id': instance.id,
            'view_mode': 'form',
            'views': [(self.env.ref('mis_builder.mis_report_instance_result_view_form').id, 'form')],
            'target': 'current',
            # mis_report_widget reads its instance from active_id (Pitfalls #183)
            'context': {'active_model': 'mis.report.instance', 'active_id': instance.id},
        }


class KswMisPeriodWizard(models.AbstractModel):
    """A statement over a date range: one column, or one per month + total."""

    _name = 'ksw.mis.period.wizard'
    _inherit = 'ksw.mis.wizard.mixin'
    _description = 'Financial statement: choose period'

    date_from = fields.Date(
        'From', required=True,
        default=lambda self: fields.Date.context_today(self).replace(month=1, day=1))
    date_to = fields.Date(
        'To', required=True, default=lambda self: fields.Date.context_today(self))
    # Each wizard declares ``layout`` with the choices that make sense for
    # its statement; 'monthly' and 'account' are the values handled here.

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

    def _open_statement(self, report_xmlid, title):
        self.ensure_one()
        return self._open_mis_instance(
            report_xmlid, f'{title} {self.date_from} - {self.date_to}', self._periods(),
            by_account=self.layout == 'account', landscape=self.layout == 'monthly')
