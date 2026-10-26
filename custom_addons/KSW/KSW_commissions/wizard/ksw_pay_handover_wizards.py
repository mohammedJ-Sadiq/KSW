"""The two questions partial approval adds to the monthly handover.

``ksw.pay.submit.wizard`` — the supervisor's: send some employees early in
one sub-batch (the month stays open), or submit the whole month and close it?

``ksw.pay.gm.review.wizard`` — the General Manager's review screen: one line
per component per employee, components in a side panel. He goes component by
component, ticks employees, and approves or returns them (with a reason);
whatever he leaves stays waiting on him. In ``reopen`` mode the same screen
sends back rows he already approved — until they are paid. Works on a
department handover and on a sub-batch alike.
"""
import calendar
from collections import OrderedDict

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.ksw_commission_lock import LOCKING_STATES
from ..models.ksw_pay_sub_batch import OPEN_STATES


class KswPaySubmitWizard(models.TransientModel):
    """Submit to GM — some employees now, or the whole month.

    The supervisor's one habit is this button, so it is also where the
    mid-month case lives. It defaults to sending a few employees early (a
    vacation, a resignation) in a **sub-batch**: one per department, days
    and reason, across every component — Hafiz's Friday Work, Location
    Allowance and Other are one handover, not three. While that sub-batch
    is being prepared the dialog offers to add to it; once it is submitted
    it is closed, and the next early handover is a new sub-batch with its
    own days and reason.

    Closing the month is the second, explicit choice — the old default,
    which shut Maintenance and Supply Chain for October seconds after their
    sub-batches had gone (KSWCO, Oct 2026).
    """
    _name = 'ksw.pay.submit.wizard'
    _description = 'Submit to GM: some employees early, or the whole month'

    submission_ids = fields.Many2many(
        'ksw.pay.submission', string='Departments', readonly=True)
    mode = fields.Selection([
        ('early', 'Send some employees now — the month stays open'),
        ('month', 'Submit the whole month and close it'),
    ], default='early', required=True)
    # A sub-batch still being prepared, offered first: one handover per
    # days and reason, not one per pick.
    open_sub_batch_ids = fields.Many2many(
        'ksw.pay.sub.batch', 'ksw_pay_submit_wizard_open_sub_rel',
        'wizard_id', 'sub_batch_id', readonly=True)
    target = fields.Selection([
        ('append', 'Add them to the open sub-batch'),
        ('new', 'Start a new sub-batch (other days or another reason)'),
    ], default='new', required=True)
    sub_batch_id = fields.Many2one(
        'ksw.pay.sub.batch', string='Sub-Batch',
        domain="[('id', 'in', open_sub_batch_ids)]")
    sub_date_from = fields.Date(
        related='sub_batch_id.date_from', string='Sub-Batch From')
    sub_date_to = fields.Date(
        related='sub_batch_id.date_to', string='Sub-Batch To')
    sub_note = fields.Text(
        related='sub_batch_id.note', string='Sub-Batch Reason')
    sub_employee_names = fields.Char(related='sub_batch_id.employee_names')
    date_from = fields.Date(string='From')
    date_to = fields.Date(string='To')
    note = fields.Char(
        string='Why Early',
        help='Why these employees cannot wait for the rest of the month — '
             'a vacation from the 12th, a resignation, ...')
    # compute_sudo: the cost centre is an HR field and the pool reads
    # hr.employee (Odoo 19 Pitfalls #34); the scope is still asked as the
    # user (#42).
    allowed_employee_ids = fields.Many2many(
        'hr.employee', 'ksw_pay_submit_wizard_allowed_emp_rel', 'wizard_id',
        'employee_id', compute='_compute_allowed_employees',
        compute_sudo=True)
    employee_ids = fields.Many2many(
        'hr.employee', 'ksw_pay_submit_wizard_employee_rel', 'wizard_id',
        'employee_id', string='Employees',
        domain="[('id', 'in', allowed_employee_ids)]")
    has_returned_sub_batch = fields.Boolean(readonly=True)
    # Opened from a component batch (Send Early): the picker offers that
    # component's people only. Whoever is picked still goes with every
    # component — one handover per employee.
    batch_id = fields.Many2one('ksw.pay.batch', readonly=True)
    batch_hint = fields.Char(compute='_compute_allowed_employees',
                             compute_sudo=True)

    @api.model
    def _open_for(self, submissions, sub_batch=None, batch=None):
        subs = submissions.sudo().sub_batch_ids
        preparing = subs.filtered(lambda s: s.state == 'draft')
        if len(submissions) != 1:
            # Appending is to one department's sub-batch.
            preparing = preparing.browse()
        sub_batch = sub_batch or preparing[:1]
        period = submissions[:1].period
        vals = {
            'submission_ids': [(6, 0, submissions.ids)],
            'open_sub_batch_ids': [(6, 0, preparing.ids)],
            'target': 'append' if sub_batch else 'new',
            'sub_batch_id': sub_batch.id if sub_batch else False,
            'has_returned_sub_batch': bool(
                subs.filtered(lambda s: s.state == 'returned')),
            'batch_id': batch.id if batch else False,
        }
        if period:
            start = period.replace(day=1)
            end = start.replace(
                day=calendar.monthrange(start.year, start.month)[1])
            today = fields.Date.context_today(self)
            vals.update(date_from=start,
                        date_to=today if start <= today <= end else end)
        wizard = self.create(vals)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Submit to the General Manager'),
            'res_model': self._name,
            'res_id': wizard.id,
            'view_mode': 'form',
            'target': 'new',
        }

    # ------------------------------------------------------------------
    # Who can be sent
    # ------------------------------------------------------------------
    def _days(self):
        self.ensure_one()
        if self.target == 'append' and self.sub_batch_id:
            return self.sub_batch_id.date_from, self.sub_batch_id.date_to
        return self.date_from, self.date_to

    @api.model
    def _open_batches_of(self, submissions):
        return submissions.sudo().batch_ids.filtered(
            lambda b: b.state == 'draft')

    @api.model
    def _drivers(self, batch):
        """Who an importer batch could fill: everyone its import reaches —
        asked as the user, so it widens by *his* reporting chain only — who
        has a BAS cost centre. A mechanic or a clerk has no trips."""
        pool = batch.with_env(self.env(su=False))._allowed_employees()
        return pool.sudo().filtered(
            lambda e: e.x_bas_driver_cost_center
            or e.x_bas_driver_cost_center_alt)

    @api.model
    def _pool(self, submissions, date_from, date_to, sub_batch=None):
        """Whoever has something to send in those days — a draft row, or
        trips still to import — and is not already in an open sub-batch
        (one handover at a time per employee)."""
        Employee = self.env['hr.employee'].sudo()
        if not (date_from and date_to):
            return Employee
        pool = Employee
        for batch in self._open_batches_of(submissions):
            pool |= self._batch_pool(batch, date_from, date_to)
        return pool - self._busy(submissions)

    @api.model
    def _batch_pool(self, batch, date_from, date_to):
        """Who one component batch has something to send for."""
        if batch.component_id.importer:
            return self._drivers(batch)
        return batch.sudo().entry_ids.filtered(
            lambda e: e.state == 'draft' and e.employee_id
            and e._within(date_from, date_to)).employee_id

    @api.model
    def _busy(self, submissions):
        return submissions.sudo().sub_batch_ids.filtered(
            lambda s: s.state in OPEN_STATES).employee_ids

    @api.depends('submission_ids', 'target', 'sub_batch_id',
                 'date_from', 'date_to', 'batch_id')
    def _compute_allowed_employees(self):
        Wizard = self.with_env(self.env(su=False))
        for wiz in self:
            date_from, date_to = wiz._days()
            submissions = wiz.sub_batch_id.submission_id \
                if wiz.target == 'append' and wiz.sub_batch_id \
                else wiz.submission_ids
            pool = Wizard._pool(submissions, date_from, date_to)
            wiz.batch_hint = False
            if wiz.batch_id and date_from and date_to:
                mine = Wizard._batch_pool(wiz.batch_id, date_from, date_to)
                pool &= mine
                # Said out loud, or an empty picker reads as a fault: they
                # are already sent, and this component goes with them.
                sent = mine & Wizard._busy(submissions)
                if sent:
                    wiz.batch_hint = _(
                        "Already in a sub-batch, with their %(what)s "
                        "entries: %(names)s",
                        what=wiz.batch_id.component_id.name,
                        names=', '.join(sent.mapped('name')))
            wiz.allowed_employee_ids = pool

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def action_confirm(self):
        """Submit & Close the Month, or Add & Send to GM."""
        self.ensure_one()
        if self.mode == 'month':
            self.submission_ids.action_submit()
            return {'type': 'ir.actions.act_window_close'}
        return self._send_early(send=True)

    def action_add(self):
        """Add to the sub-batch and keep it open for more."""
        self.ensure_one()
        return self._send_early(send=False)

    def _send_early(self, send):
        if not self.employee_ids:
            raise UserError(_("Pick at least one employee to send."))
        if self.target == 'append':
            if not self.sub_batch_id:
                raise UserError(_("Choose the sub-batch to add them to."))
            if self.sub_batch_id.state != 'draft':
                raise UserError(_(
                    "%(name)s has already been submitted, so nobody can be "
                    "added to it. Start a new sub-batch with its own days "
                    "and reason.", name=self.sub_batch_id.name))
            subs = self.sub_batch_id
        else:
            if not (self.note or '').strip():
                raise UserError(_(
                    "Say why these employees are sent early — the General "
                    "Manager sees it on the sub-batch."))
            if not (self.date_from and self.date_to):
                raise UserError(_("Give the days this sub-batch covers."))
            # One per department, with the employees who belong to it.
            subs = self.env['ksw.pay.sub.batch']
            for submission in self.submission_ids:
                picked = self.employee_ids.sudo() & self._pool(
                    submission, self.date_from, self.date_to)
                if not picked:
                    continue
                subs |= self.env['ksw.pay.sub.batch'].create({
                    'submission_id': submission.id,
                    'date_from': self.date_from,
                    'date_to': self.date_to,
                    'note': self.note,
                })
        skipped = []
        for sub in subs:
            skipped += sub._add_employees(self.employee_ids)
        subs = subs.filtered('employee_ids')
        if not subs:
            date_from, date_to = self._days()
            raise UserError(_(
                "Nothing was sent — none of the picked employees has an "
                "open entry in %(start)s – %(end)s.%(why)s",
                start=fields.Date.to_string(date_from),
                end=fields.Date.to_string(date_to),
                why=('\n\n' + '\n'.join(skipped)) if skipped else ''))
        if send:
            subs.action_submit()
        missed = self.employee_ids.sudo() - subs.employee_ids
        names = ', '.join(subs.mapped('name'))
        message = (_(
            "%(subs)s sent to the General Manager. Everything else in the "
            "month stays open.", subs=names) if send else _(
            "Added to %(subs)s. Add more people or press Submit to GM on it "
            "when it is complete.", subs=names))
        if missed:
            message += '\n\n' + _(
                "Nothing to send for: %(names)s",
                names=', '.join(missed.mapped('name')))
        if skipped:
            message += '\n\n' + '\n'.join(skipped)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Sent Early') if send else _('Added'),
                'message': message,
                'type': 'warning' if (missed or skipped) else 'success',
                'sticky': bool(missed or skipped),
                'next': {
                    'type': 'ir.actions.act_window',
                    'res_model': 'ksw.pay.sub.batch',
                    'res_id': subs[:1].id,
                    'view_mode': 'form',
                    'views': [(False, 'form')],
                    'target': 'current',
                },
            },
        }


class KswPayGmReviewWizard(models.TransientModel):
    """The GM's review of one handover — the header of the review screen.

    Not a dialog: the lines open as a list with the components in a side
    panel, so he goes component by component, ticks employees, and approves
    or returns them; whatever he does not touch stays waiting on him.
    """
    _name = 'ksw.pay.gm.review.wizard'
    _description = 'GM review of a handover, component by component'

    mode = fields.Selection([
        ('review', 'Approve or return'),
        ('reopen', 'Reopen approved entries'),
    ], required=True, readonly=True)
    submission_id = fields.Many2one('ksw.pay.submission', readonly=True)
    sub_batch_id = fields.Many2one('ksw.pay.sub.batch', readonly=True)
    line_ids = fields.One2many('ksw.pay.gm.review.line', 'wizard_id')

    @api.model
    def _open_for(self, container, mode):
        """One line per (component, employee) of what ``container`` has in
        front of the GM, opened as the review screen."""
        if container.run_id.state in LOCKING_STATES:
            raise UserError(_(
                "%(month)s has already been finalised. Reopen the month "
                "first.", month=container.run_id.display_name))
        entries = container._review_entries(mode)
        if not entries:
            raise UserError(
                _("There are no approved entries here to reopen.")
                if mode == 'reopen' else
                _("There is nothing waiting for your approval here."))
        groups = OrderedDict()
        for entry in entries.sorted(
                lambda e: (e.component_id.sequence, e.component_id.id,
                           e.employee_id.name or '', e.batch_id.id)):
            key = (entry.batch_id.id, entry.employee_id.id)
            groups.setdefault(key, self.env['ksw.pay.entry'])
            groups[key] |= entry
        lines = []
        for rows in groups.values():
            first = rows[0]
            lines.append((0, 0, {
                'component_id': first.component_id.id,
                'employee_name': first.employee_id.sudo().name,
                'batch_name': first.batch_id.name,
                'sub_batch_name': ', '.join(
                    rows.x_sub_batch_id.filtered('submitted_date')
                    .mapped('name')),
                'entry_ids': [(6, 0, rows.ids)],
                'amount': sum(rows.mapped('amount')),
            }))
        vals = {'mode': mode, 'line_ids': lines}
        if container._name == 'ksw.pay.sub.batch':
            vals['sub_batch_id'] = container.id
        else:
            vals['submission_id'] = container.id
        wizard = self.create(vals)
        view = 'view_ksw_pay_gm_review_line_list' if mode == 'review' \
            else 'view_ksw_pay_gm_review_line_list_reopen'
        return {
            'type': 'ir.actions.act_window',
            'name': (_('Review — %(name)s', name=container.display_name)
                     if mode == 'review' else
                     _('Reopen — %(name)s', name=container.display_name)),
            'res_model': 'ksw.pay.gm.review.line',
            'view_mode': 'list',
            'views': [(self.env.ref('KSW_commissions.' + view).id, 'list')],
            'search_view_id': self.env.ref(
                'KSW_commissions.view_ksw_pay_gm_review_line_search').id,
            'domain': [('wizard_id', '=', wizard.id)],
            'target': 'current',
        }

    def _container(self):
        return self.sub_batch_id or self.submission_id

    def _check_gm(self):
        if self.sub_batch_id:
            self.sub_batch_id._check_gm()
        else:
            self.submission_id._check_is_my_department()

    def _act(self, lines, action, reason=None):
        """Approve, return or reopen the rows behind ``lines``.

        The one place the review screen changes anything; its lines are
        removed afterwards so the screen shows only what is still undecided.
        """
        self.ensure_one()
        self._check_gm()
        container = self._container()
        if container.run_id.state in LOCKING_STATES:
            raise UserError(_(
                "%(month)s has already been finalised. Reopen the month "
                "first.", month=container.run_id.display_name))
        if not lines:
            raise UserError(_("Tick the employees first."))
        if action != 'approve':
            reason = (reason or '').strip()
            if not reason:
                raise UserError(_(
                    "Say why — the supervisor needs to know what to fix."))
        rows = lines.entry_ids.sudo()
        if action == 'approve':
            moved = rows._wf_approve()
        elif action == 'return':
            moved = rows._wf_return(reason)
        else:
            moved = rows._wf_reopen(reason)
        if not moved:
            raise UserError(_(
                "Those entries have already moved on — reload the page."))
        if action != 'approve':
            for batch in moved.batch_id:
                batch.with_context(ksw_entry_sync=True).write(
                    {'return_reason': reason})
            moved.x_sub_batch_id.write({'return_reason': reason})
        moved._sync_containers()
        if action == 'approve' and container._name == 'ksw.pay.sub.batch':
            container.sudo().write({
                'approved_by': self.env.uid,
                'approved_date': fields.Datetime.now()})
        if action != 'approve':
            moved.batch_id.submission_id.filtered(
                lambda s: s.state == 'returned'
            ).sudo().write({'returned_by': self.env.uid,
                            'return_reason': reason})
        self._announce(container, lines, action, reason)
        lines.unlink()
        runs = moved.batch_id.submission_id.run_id
        runs._sync_state()
        runs._refresh_register()
        # Approving the last thing a department had waiting is the same
        # decision as pressing Approve on it (submission.action_approve).
        if action == 'approve' and container._name == 'ksw.pay.submission':
            runs._notify_ready_to_close()
        return True

    def _announce(self, container, lines, action, reason):
        title = {
            'approve': _('✅ Approved'),
            'return': _('↩ Returned for correction'),
            'reopen': _('🔓 Reopened for correction'),
        }[action]
        body = Markup(
            '<strong>%(title)s</strong><br/>'
            '<b>%(l_by)s</b> %(user)s<br/>'
        ) % {'title': title, 'l_by': _('By:'), 'user': self.env.user.name}
        if reason:
            body += Markup('<b>%(l_reason)s</b> %(reason)s<br/>') % {
                'l_reason': _('Reason:'), 'reason': reason}
        body += Markup('<ul>')
        for line in lines:
            body += Markup('<li>%(what)s — %(emp)s (%(n)s, %(amt).2f)</li>') % {
                'emp': line.employee_name, 'what': line.component_id.name,
                'n': line.entry_count, 'amt': line.amount}
        body += Markup('</ul>')
        container.sudo().message_post(
            body=body,
            partner_ids=container._supervisor_partners().ids,
            subtype_xmlid='mail.mt_comment',
        )


class KswPayGmReviewLine(models.TransientModel):
    _name = 'ksw.pay.gm.review.line'
    _description = 'One employee, one component, in the GM review'
    _order = 'id'

    wizard_id = fields.Many2one(
        'ksw.pay.gm.review.wizard', required=True, ondelete='cascade')
    component_id = fields.Many2one(
        'ksw.pay.component', string='Component', readonly=True)
    # A name, not a relation: the GM's hr.employee rule covers his own
    # departments, and a supervisor's reporting chain can reach into
    # another — a Many2one there could fail to render for him.
    employee_name = fields.Char(string='Employee', readonly=True)
    batch_name = fields.Char(string='Batch', readonly=True)
    sub_batch_name = fields.Char(string='Sub-Batch', readonly=True)
    entry_ids = fields.Many2many(
        'ksw.pay.entry', 'ksw_pay_gm_review_line_entry_rel',
        'line_id', 'entry_id', readonly=True)
    entry_count = fields.Integer(
        compute='_compute_entry_count', string='Entries')
    amount = fields.Float(readonly=True, digits=(16, 2))

    @api.depends('entry_ids')
    def _compute_entry_count(self):
        for rec in self:
            rec.entry_count = len(rec.entry_ids)

    def _wizard(self):
        wizard = self.wizard_id
        if len(wizard) != 1:
            raise UserError(_("Tick the employees first."))
        return wizard

    def action_approve(self):
        """Approve the ticked employees; the rest stay waiting."""
        self._wizard()._act(self, 'approve')
        return {'type': 'ir.actions.client', 'tag': 'soft_reload'}

    def action_return(self):
        """Ask for the reason, then send the ticked employees back."""
        return self._open_reason('return')

    def action_reopen(self):
        return self._open_reason('reopen')

    def _open_reason(self, action):
        self._wizard()
        dialog = self.env['ksw.pay.gm.review.reason'].create({
            'action': action, 'line_ids': [(6, 0, self.ids)]})
        return {
            'type': 'ir.actions.act_window',
            'name': _('Return for Correction') if action == 'return'
            else _('Reopen Approved Entries'),
            'res_model': dialog._name,
            'res_id': dialog.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_open_entries(self):
        """The occurrences behind this line."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': '%s — %s' % (self.component_id.name, self.employee_name),
            'res_model': 'ksw.pay.entry',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self.entry_ids.ids)],
            'target': 'new',
        }


class KswPayGmReviewReason(models.TransientModel):
    _name = 'ksw.pay.gm.review.reason'
    _description = 'Why the GM is sending these back'

    action = fields.Selection(
        [('return', 'Return'), ('reopen', 'Reopen')], required=True,
        readonly=True)
    line_ids = fields.Many2many(
        'ksw.pay.gm.review.line', 'ksw_pay_gm_review_reason_line_rel',
        'reason_id', 'line_id', readonly=True)
    # Required in the view and in _act, not here: the dialog is created
    # before the GM has typed anything.
    reason = fields.Text(
        help='The supervisor sees this on every row you send back.')

    def action_confirm(self):
        self.ensure_one()
        self.line_ids._wizard()._act(self.line_ids, self.action, self.reason)
        return {'type': 'ir.actions.act_window_close'}
