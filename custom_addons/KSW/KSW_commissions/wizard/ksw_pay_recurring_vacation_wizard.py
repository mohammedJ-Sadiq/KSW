"""Add Recurring… — the employees whose vacation is still being approved.

From GM final approval an employee is 'On Vacation' and the hold leaves him
out of the pull on its own. Before that — DM, HR, GM initial, Accounting,
GM final — the request may still be refused, so nothing is decided for him:
the supervisor is shown everybody in that position at once and ticks whom
to add. Everyone else is pulled in as usual.

Ticked by default, i.e. what the button did before: a row added for
somebody whose vacation then goes through is flagged on the batch and
refused at submit, while a row left out for somebody whose vacation is
refused is simply never paid.
"""
from odoo import _, api, fields, models

from ..models.ksw_vacation_hold import pending_vacations


class KswPayRecurringVacationWizard(models.TransientModel):
    _name = 'ksw.pay.recurring.vacation.wizard'
    _description = 'Add Recurring: employees with a vacation pending approval'

    batch_id = fields.Many2one(
        'ksw.pay.batch', required=True, readonly=True, ondelete='cascade')
    line_ids = fields.One2many(
        'ksw.pay.recurring.vacation.wizard.line', 'wizard_id')

    @api.model
    def _open_for_batch(self, batch):
        due = self.env['ksw.pay.recurring']._due_for_batch(batch)
        pending = pending_vacations(self.env, due.employee_id, batch.period)
        wizard = self.create({
            'batch_id': batch.id,
            'line_ids': [
                (0, 0, self._line_vals(leave))
                for leave in sorted(
                    pending.values(),
                    key=lambda l: l.employee_id.sudo().name or '')
            ],
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _('Pending Vacations'),
            'res_model': self._name,
            'res_id': wizard.id,
            'view_mode': 'form',
            'target': 'new',
        }

    @api.model
    def _line_vals(self, leave):
        # Plain values, not a link to the request: a supervisor has no
        # hr.leave access, and a Many2one to it would fail on display.
        leave = leave.sudo()
        step = False
        if leave.x_annual_approval_state:
            step = dict(leave._fields['x_annual_approval_state']
                        ._description_selection(self.env)
                        ).get(leave.x_annual_approval_state)
        return {
            'employee_id': leave.employee_id.id,
            'leave_type': leave.holiday_status_id.display_name,
            'date_from': leave.request_date_from,
            'date_to': leave.request_date_to,
            'step': step or _('Awaiting approval'),
        }

    def action_confirm(self):
        self.ensure_one()
        skip = self.line_ids.filtered(lambda l: not l.add).employee_id
        created = self.env['ksw.pay.recurring']._apply_to_batch(
            self.batch_id, skip_employee_ids=skip.ids)
        return self.batch_id._recurring_added(created)


class KswPayRecurringVacationWizardLine(models.TransientModel):
    _name = 'ksw.pay.recurring.vacation.wizard.line'
    _description = 'Add Recurring: one employee with a pending vacation'

    wizard_id = fields.Many2one(
        'ksw.pay.recurring.vacation.wizard', required=True,
        ondelete='cascade')
    employee_id = fields.Many2one('hr.employee', readonly=True)
    leave_type = fields.Char(string='Leave', readonly=True)
    date_from = fields.Date(string='From', readonly=True)
    date_to = fields.Date(string='To', readonly=True)
    step = fields.Char(string='Waiting For', readonly=True)
    add = fields.Boolean(string='Add', default=True)
