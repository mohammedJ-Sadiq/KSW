from dateutil.relativedelta import relativedelta

from odoo import api, fields, models


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    x_deduction_count = fields.Integer(
        string='Active Deductions',
        compute='_compute_deduction_count',
        groups='hr.group_hr_user',
    )
    x_deduction_monthly_total = fields.Monetary(
        string='Monthly Deduction Total',
        compute='_compute_deduction_count',
        groups='hr.group_hr_user',
        currency_field='x_deduction_currency_id',
        help='Pending installments this month\'s payroll will collect: '
             'those scheduled for the current month plus any still pending '
             'from earlier months.',
    )
    x_deduction_outstanding_total = fields.Monetary(
        string='Outstanding Deduction Total',
        compute='_compute_deduction_count',
        groups='hr.group_hr_user',
        currency_field='x_deduction_currency_id',
        help='All still-pending installments across every active deduction, '
             'regardless of the month they fall in.',
    )
    x_deduction_currency_id = fields.Many2one(
        'res.currency',
        compute='_compute_deduction_count',
        groups='hr.group_hr_user',
    )
    x_loan_acc_no = fields.Char(
        string='Loan Acc No. in Bas',
        groups='hr.group_hr_user',
        help='Employee loan account number in BAS (bank loan/financing system).',
    )

    def _compute_deduction_count(self):
        company_currency = self.env.company.currency_id
        today = fields.Date.context_today(self)
        period_start = today.replace(day=1)
        period_end = period_start + relativedelta(months=1, days=-1)
        for emp in self:
            active = self.env['ksw.deduction'].sudo().search([
                ('employee_id', '=', emp.id),
                ('state', '=', 'active'),
            ])
            emp.x_deduction_count = len(active)
            # `monthly` sums the pending installments this month's payroll will
            # actually collect: the ones scheduled for the current month PLUS
            # any left pending from an earlier month. A payslip that could not
            # afford an installment leaves the remainder pending with its
            # ORIGINAL period (see `_settle_payslip_lines`), and
            # `hr.payslip._ksw_pending_lines_domain` picks up everything with
            # `period_date <= date_to` — so the same `<=` rule is used here.
            # `outstanding` sums every pending installment regardless of month
            # (what the employee still owes in total).
            monthly = 0.0
            outstanding = 0.0
            for ded in active:
                for line in ded.line_ids:
                    if line.state != 'pending':
                        continue
                    outstanding += line.amount
                    if line.period_date and line.period_date <= period_end:
                        monthly += line.amount
            emp.x_deduction_monthly_total = monthly
            emp.x_deduction_outstanding_total = outstanding
            emp.x_deduction_currency_id = company_currency

    def action_view_deductions(self):
        self.ensure_one()
        return {
            'name': 'Deductions of %s' % self.name,
            'type': 'ir.actions.act_window',
            'res_model': 'ksw.deduction',
            'view_mode': 'list,form',
            'domain': [('employee_id', '=', self.id)],
            'context': {'default_employee_id': self.id},
        }

