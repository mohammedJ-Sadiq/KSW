"""One employee's month in one department handover — the reviewer's page.

The submission's *Employees* button used to open every entry grouped per
employee. That mixed components in one list, so the quantity total added
overtime hours to Friday days to meal counts — a number that means nothing —
and moving to the next person meant going back and opening another group.

This is a read-only SQL view with one row per (submission, employee). Its
form shows the entries split into one section per component, each with its
own subtotal in that component's unit, and the standard form pager walks the
reviewer from one employee to the next.

The view only supplies the row; every figure on it is computed from
``ksw.pay.entry`` searched as the viewing user, so entry record rules still
decide what a reviewer is shown. Row visibility follows the submission's own
rules (security.xml).
"""
import datetime

from odoo import _, api, fields, models, tools
from odoo.tools import SQL


class KswPayEmployeeReview(models.Model):
    _name = 'ksw.pay.employee.review'
    _description = 'KSW Pay Employee Review'
    _auto = False
    _order = 'submission_id, employee_name, id'
    _rec_name = 'employee_id'

    submission_id = fields.Many2one('ksw.pay.submission', readonly=True)
    employee_id = fields.Many2one('hr.employee', readonly=True)
    employee_name = fields.Char(readonly=True)
    currency_id = fields.Many2one(
        related='submission_id.currency_id', readonly=True)

    entry_ids = fields.Many2many(
        'ksw.pay.entry', compute='_compute_review', string='Entry Lines')
    entry_count = fields.Integer(compute='_compute_review', string='Entries')
    component_names = fields.Char(
        compute='_compute_review', string='Components')
    amount = fields.Monetary(compute='_compute_review', string='Total')
    pending_count = fields.Integer(
        compute='_compute_review', string='Waiting on the GM')
    vacation_hold = fields.Char(
        compute='_compute_review', string='On Vacation')
    sections_html = fields.Html(
        compute='_compute_review', sanitize=False, string='Sections')

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        # id = the group's lowest entry id: unique (an entry belongs to one
        # submission and one employee) and stable while that entry lives.
        self.env.cr.execute(SQL("""
            CREATE OR REPLACE VIEW %s AS (
                SELECT MIN(e.id)        AS id,
                       b.submission_id  AS submission_id,
                       e.employee_id    AS employee_id,
                       MIN(emp.name)    AS employee_name
                  FROM ksw_pay_entry e
                  JOIN ksw_pay_batch b ON b.id = e.batch_id
                  JOIN hr_employee emp ON emp.id = e.employee_id
                 WHERE b.submission_id IS NOT NULL
              GROUP BY b.submission_id, e.employee_id
            )""", SQL.identifier(self._table)))

    def _visible_entries(self):
        """The entries behind this row, as the viewing user may see them."""
        self.ensure_one()
        return self.env['ksw.pay.entry'].search([
            ('batch_id.submission_id', '=', self.submission_id.id),
            ('employee_id', '=', self.employee_id.id),
        ])

    @api.depends_context('uid')
    @api.depends('submission_id', 'employee_id')
    def _compute_review(self):
        for rec in self:
            entries = rec._visible_entries()
            components = entries.component_id.sorted()
            rec.entry_ids = entries
            rec.entry_count = len(entries)
            rec.component_names = ', '.join(components.mapped('name'))
            rec.amount = sum(entries.mapped('amount'))
            rec.pending_count = len(entries.filtered(
                lambda e: e.state == 'submitted'))
            rec.vacation_hold = next(
                (h for h in entries.mapped('x_vacation_hold') if h), False)
            rec.sections_html = rec._render_sections(entries, components)

    def _render_sections(self, entries, components):
        state_labels = dict(
            entries._fields['state']._description_selection(self.env))
        sections = []
        for component in components:
            rows = entries.filtered(lambda e: e.component_id == component)
            sections.append({
                'component': component,
                'qty_label': component.qty_label or _('Quantity'),
                'show_option': any(rows.mapped('option_id')),
                'show_date': any(rows.mapped('date')),
                'show_reason': any(rows.mapped('reason')),
                'rows': rows.sorted(
                    lambda e: (e.date or datetime.date.min, e.id)),
                'quantity': sum(rows.mapped('quantity')),
                'amount': sum(rows.mapped('amount')),
            })
        return self.env['ir.qweb']._render(
            'KSW_commissions.employee_review_sections', {
                'sections': sections,
                'state_labels': state_labels,
                'total': sum(entries.mapped('amount')),
                'format_qty': lambda value: (
                    '%.2f' % value).rstrip('0').rstrip('.'),
                'format_date': lambda value: tools.format_date(
                    self.env, value),
                'format_amount': lambda value: tools.format_amount(
                    self.env, value,
                    self.currency_id or self.env.company.currency_id),
            })
