"""What the import left out, and why — kept on the batch.

A toast says it once. The supervisor presses Import, reads "7 were on
vacation this month and were skipped", clicks away, and an hour later has
no way to answer "why is the department four drivers short?" except by
pressing Import again. The payslip batch already learned this
(``ksw.payslip.run.skip.line``): a run that silently drops people needs a
log on the document, not a message in the corner.

Same split as payroll, because the two outcomes need different reactions:

  * **skipped**  — no line at all. Fix the cause and import again.
  * **warning**  — the line IS there, but something about it is unusual
                   and the figure should be looked at before approval.
"""
from odoo import _, api, fields, models

#: code -> (label, outcome). The label is what the badge reads, so each
#: one says what happened rather than naming an internal category.
SKIP_REASONS = {
    'on_vacation': (
        'Held — settled on a vacation request', 'skipped'),
    'no_cost_centre': (
        'Not imported — no BAS cost centre', 'skipped'),
    'no_data': (
        'Not imported — no trips in BAS', 'skipped'),
    'kept': (
        'Left unchanged — already had a line', 'warning'),
    'part_month': (
        'Imported from the return date only', 'warning'),
    'no_attendance': (
        'Imported — full requirement applied', 'warning'),
    'no_invoice_factor': (
        'Not imported — no رد الفاتورة on any load', 'skipped'),
    'part_invoice_factor': (
        'Imported — some loads carry no رد الفاتورة', 'warning'),
    'cash_band': (
        'Cash loads re-rated from the amount band', 'warning'),
    'out_of_scope': (
        'NOT refreshed — outside this batch’s scope', 'warning'),
}


class KswPayBatchSkipLine(models.Model):
    _name = 'ksw.pay.batch.skip.line'
    _description = 'KSW Pay Batch — Not Imported Log'
    _order = 'outcome, employee_id'

    batch_id = fields.Many2one(
        'ksw.pay.batch', string='Batch',
        required=True, ondelete='cascade', index=True,
    )
    employee_id = fields.Many2one(
        'hr.employee', string='Employee',
        required=True, ondelete='cascade',
    )
    line_type = fields.Selection(
        [(code, label) for code, (label, _o) in SKIP_REASONS.items()],
        string='What Happened', required=True,
    )
    outcome = fields.Selection([
        ('skipped', 'Not imported'),
        ('warning', 'Imported — needs review'),
    ], string='Outcome', required=True, default='skipped', index=True)
    reason = fields.Char(
        string='Reason', required=True,
        help='The specific explanation for this employee, with the dates '
             'and figures behind it.',
    )

    @api.depends('employee_id', 'line_type')
    def _compute_display_name(self):
        labels = dict(self._fields['line_type'].selection)
        for rec in self:
            rec.display_name = '%s — %s' % (
                rec.employee_id.sudo().display_name or '',
                labels.get(rec.line_type, ''))

    @api.model
    def _log(self, batch, employee, code, reason):
        """Build one row's values. ``outcome`` follows the code, never the
        caller — otherwise two call sites classify the same thing
        differently and the list stops meaning anything."""
        return {
            'batch_id': batch.id,
            'employee_id': employee.id,
            'line_type': code,
            'outcome': SKIP_REASONS[code][1],
            'reason': reason,
        }
