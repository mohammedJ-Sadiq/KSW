"""Send Early… — some employees of one component, for some days, to the GM now.

Opened from inside a component's batch (Driver Trips · Sep, Location
Allowance · Sep). The supervisor picks the days and the employees and
presses one button: the sub-batch is made, Driver Trips is imported for the
picked drivers and those days only, and it goes to the General Manager. No
separate save, import or submit, and nothing that has to be done in order.

It replaced a sub-batch that took an employee's rows from every component of
the month and was filled in several steps. Supervisors think of it as "a
sub-batch for the trips" — one component — and the cross-component picker
offered a Location Allowance sub-batch the 63 drivers the trips import had
just filled.
"""
import calendar

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class KswPaySendEarlyWizard(models.TransientModel):
    _name = 'ksw.pay.send.early.wizard'
    _description = 'Send some employees of a component to the GM early'

    batch_id = fields.Many2one(
        'ksw.pay.batch', required=True, readonly=True, ondelete='cascade')
    component_id = fields.Many2one(
        related='batch_id.component_id', string='Component')
    is_import = fields.Boolean(compute='_compute_is_import')
    date_from = fields.Date(string='From', required=True)
    date_to = fields.Date(string='To', required=True)
    note = fields.Char(
        string='Why Early',
        help='Why these employees cannot wait for the rest of the month — '
             'a vacation from the 12th, a resignation, ...')
    # Who can be picked. compute_sudo: the cost centre is an HR field and
    # the pool helper reads hr.employee (Odoo 19 Pitfalls #34); the pool
    # itself is still asked as the user (#42).
    allowed_employee_ids = fields.Many2many(
        'hr.employee', 'ksw_pay_send_early_allowed_rel', 'wizard_id',
        'employee_id', compute='_compute_allowed_employees',
        compute_sudo=True)
    employee_ids = fields.Many2many(
        'hr.employee', 'ksw_pay_send_early_employee_rel', 'wizard_id',
        'employee_id', string='Employees',
        domain="[('id', 'in', allowed_employee_ids)]")

    @api.depends('batch_id')
    def _compute_is_import(self):
        for wiz in self:
            wiz.is_import = bool(wiz.batch_id.component_id.importer)

    @api.model_create_multi
    def create(self, vals_list):
        """1st of the month to today, or to the month's end when today is
        not in it — the usual "everything so far"."""
        for vals in vals_list:
            batch = self.env['ksw.pay.batch'].sudo().browse(
                vals.get('batch_id'))
            if not batch.period:
                continue
            start = batch.period.replace(day=1)
            end = start.replace(
                day=calendar.monthrange(start.year, start.month)[1])
            today = fields.Date.context_today(self)
            vals.setdefault('date_from', start)
            vals.setdefault('date_to', today if start <= today <= end else end)
        return super().create(vals_list)

    @api.depends('batch_id', 'date_from', 'date_to')
    def _compute_allowed_employees(self):
        for wiz in self:
            wiz.allowed_employee_ids = wiz._pool()

    def _busy(self):
        """Employees already in an open sub-batch of this component."""
        self.ensure_one()
        return self.env['ksw.pay.sub.batch'].sudo().search([
            ('batch_id', '=', self.batch_id.id),
            ('state', 'in', ('draft', 'returned', 'submitted')),
        ]).employee_ids

    def _pool(self):
        self.ensure_one()
        batch = self.batch_id.sudo()
        Employee = self.env['hr.employee'].sudo()
        if not batch:
            return Employee
        if batch.component_id.importer:
            # Everyone the batch's import could fill — asked as the user,
            # so it widens by *his* reporting chain only — who has a BAS
            # cost centre. A mechanic or a clerk has no trips to pick.
            pool = batch.with_env(self.env(su=False))._allowed_employees()
            pool = pool.sudo().filtered(
                lambda e: e.x_bas_driver_cost_center
                or e.x_bas_driver_cost_center_alt)
        else:
            # Typed components: whoever has a draft row here in those days.
            rows = batch.entry_ids.filtered(
                lambda e: e.state == 'draft' and e.employee_id
                and e._within(self.date_from, self.date_to))
            pool = rows.employee_id
        return pool - self._busy()

    def action_send(self):
        """Make the sub-batch, fill it, and hand it to the GM — in one go.

        All or nothing: if nothing can be sent (BAS has no loads for the
        picked drivers, nobody has a row in those days), it raises and
        nothing is left behind — no empty sub-batch, no half import.
        """
        self.ensure_one()
        batch = self.batch_id
        start = batch.period.replace(day=1)
        end = start.replace(
            day=calendar.monthrange(start.year, start.month)[1])
        if not (start <= self.date_from <= self.date_to <= end):
            raise ValidationError(_(
                "The days must run forward and stay inside %(month)s.",
                month=start.strftime('%B %Y')))
        batch._check_editable_batch(_("Sending employees early"))
        batch.submission_id._check_mine()
        picked = self.employee_ids.sudo() & self._pool()
        if not picked:
            raise UserError(_("Pick at least one employee to send."))

        sub = self.env['ksw.pay.sub.batch'].create({
            'batch_id': batch.id,
            'date_from': self.date_from,
            'date_to': self.date_to,
            'note': self.note,
        })
        message = False
        if batch.component_id.importer:
            # Through the batch's one import door, as the supervisor: the
            # period lock, draft check and employee scope still apply.
            result = batch.with_env(self.env).with_context(
                ksw_pay_importing=True)._import_bas_trips(
                employees=picked, window=(self.date_from, self.date_to),
                sub_batch=sub)
            message = result['params']['message']
            got_rows = batch.sudo().entry_ids.filtered(
                lambda e: e.employee_id in picked
                and (e.x_window_from, e.x_window_to)
                == (self.date_from, self.date_to)).employee_id
            if not got_rows:
                raise UserError(_(
                    "Nothing was sent — none of the picked drivers could be "
                    "imported for those days:\n\n%(why)s", why=message))
            picked = got_rows
        sub.write({'employee_ids': [(6, 0, picked.ids)]})
        sub.action_submit()

        summary = _(
            "%(name)s sent to the General Manager: %(count)s employee(s), "
            "%(start)s – %(end)s.", name=sub.name, count=len(picked),
            start=fields.Date.to_string(self.date_from),
            end=fields.Date.to_string(self.date_to))
        if message:
            summary += '\n\n' + message
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Sent Early'),
                'message': summary,
                'type': 'success',
                # Sticky when the import left somebody out: that list is
                # what the supervisor has to act on.
                'sticky': bool(message and '\n' in message),
                'next': {
                    'type': 'ir.actions.act_window',
                    'res_model': 'ksw.pay.sub.batch',
                    'res_id': sub.id,
                    'view_mode': 'form',
                    'views': [(False, 'form')],
                    'target': 'current',
                },
            },
        }
