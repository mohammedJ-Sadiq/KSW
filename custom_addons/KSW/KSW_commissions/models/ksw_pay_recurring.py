"""KSW Recurring Pay Entry — SAP infotype 0014, the repeating half.

A lot of extra pay is the same every month: a mobile-phone allowance, a
project-management allowance. Recording it by hand twelve times a year is what
the old commission template existed for.

A recurring entry says "this employee gets this component, at this quantity,
between these dates". The supervisor pulls them into the month's batch with one
button, then adjusts anything that changed. It is deliberately a *pull*, not an
automatic posting — the supervisor stays responsible for what he submits.
"""
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class KswPayRecurring(models.Model):
    _name = 'ksw.pay.recurring'
    _description = 'KSW Recurring Pay Entry'
    _order = 'component_id, employee_id'

    employee_id = fields.Many2one(
        'hr.employee', required=True, ondelete='cascade', index=True,
    )
    component_id = fields.Many2one(
        'ksw.pay.component', required=True, ondelete='cascade', index=True,
    )
    quantity = fields.Float(default=1.0, digits=(16, 2))
    amount = fields.Monetary(
        help='For fixed-amount components, what to pay each month.',
    )
    currency_id = fields.Many2one(
        'res.currency', required=True,
        default=lambda s: s.env.company.currency_id,
    )
    reason = fields.Char()
    date_from = fields.Date(
        required=True, default=lambda s: fields.Date.context_today(s).replace(day=1),
    )
    date_to = fields.Date(help='Leave empty to run indefinitely.')
    active = fields.Boolean(default=True)

    department_id = fields.Many2one(
        related='employee_id.department_id', store=True, readonly=True,
    )

    _unique_employee_component = models.Constraint(
        'UNIQUE(employee_id, component_id, date_from)',
        'This employee already has a recurring entry for that component '
        'starting on that date.',
    )

    @api.constrains('date_from', 'date_to')
    def _check_dates(self):
        for rec in self:
            if rec.date_to and rec.date_to < rec.date_from:
                raise ValidationError(_(
                    "The end date cannot be before the start date."))

    @api.depends('employee_id', 'component_id')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = '%s — %s' % (
                rec.employee_id.display_name or '',
                rec.component_id.name or '')

    # ------------------------------------------------------------------
    @api.model
    def _apply_to_batch(self, batch):
        """Create the missing entries for ``batch``'s component and month.

        Idempotent: an employee who already has an entry in this batch is
        skipped, so the button can be pressed twice without duplicating.
        """
        period = batch.period
        domain = [
            ('component_id', '=', batch.component_id.id),
            ('date_from', '<=', period),
            '|', ('date_to', '=', False), ('date_to', '>=', period),
        ]
        if batch.department_id:
            domain.append(('employee_id.department_id', '=',
                           batch.department_id.id))
        recurring = self.search(domain)
        if not recurring:
            return self.env['ksw.pay.entry']

        already = set(batch.entry_ids.mapped('employee_id').ids)
        # A recurring entry is company-wide configuration; this batch is not.
        # Pull in only the people it actually covers, or the entry guard
        # would reject the whole button for one out-of-scope row.
        in_scope = set(batch._allowed_employees().ids)
        vals_list = []
        for rec in recurring:
            if rec.employee_id.id in already:
                continue
            if rec.employee_id.id not in in_scope:
                continue
            vals = {
                'batch_id': batch.id,
                'employee_id': rec.employee_id.id,
                'quantity': rec.quantity,
                'reason': rec.reason or False,
            }
            if batch.component_id.calculation == 'fixed':
                vals['amount'] = rec.amount
            if batch.component_id.needs_date:
                vals['date'] = period
            vals_list.append(vals)
        if not vals_list:
            return self.env['ksw.pay.entry']
        return self.env['ksw.pay.entry'].create(vals_list)
