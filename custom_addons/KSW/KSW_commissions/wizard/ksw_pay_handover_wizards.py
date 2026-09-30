"""The two questions partial approval adds to the monthly handover.

``ksw.pay.submit.wizard`` — the supervisor's: every component, or only the
components (and sub-batches) he ticks? The rest stays open for him.

``ksw.pay.gm.review.wizard`` — the General Manager's review screen: one line
per component per employee, components in a side panel. He goes component by
component, ticks employees, and approves or returns them (with a reason);
whatever he leaves stays waiting on him. In ``reopen`` mode the same screen
sends back rows he already approved — until they are paid. Works on a
department handover and on a sub-batch alike.
"""
from collections import OrderedDict

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.ksw_commission_lock import LOCKING_STATES


class KswPaySubmitWizard(models.TransientModel):
    _name = 'ksw.pay.submit.wizard'
    _description = 'Submit the whole department, or only its sub-batches?'

    submission_ids = fields.Many2many(
        'ksw.pay.submission', string='Departments', readonly=True)
    mode = fields.Selection([
        ('all', 'Every component — everything, sub-batches included'),
        ('some', 'Only the components I tick — the rest stays open'),
    ], default='all', required=True)
    # Components, not batches: that is what the supervisor thinks in —
    # Overtime, Meals, Fridays. A tick sends that component's open batch in
    # each of his departments.
    allowed_component_ids = fields.Many2many(
        'ksw.pay.component', 'ksw_pay_submit_wizard_allowed_comp_rel',
        'wizard_id', 'component_id', readonly=True)
    component_ids = fields.Many2many(
        'ksw.pay.component', 'ksw_pay_submit_wizard_comp_rel',
        'wizard_id', 'component_id', string='Components',
        domain="[('id', 'in', allowed_component_ids)]")
    sub_batch_ids = fields.Many2many(
        'ksw.pay.sub.batch', string='Sub-Batches',
        domain="[('submission_id', 'in', submission_ids), "
               "('state', 'in', ('draft', 'returned'))]")
    has_open_sub_batch = fields.Boolean(readonly=True)

    @api.model
    def _open_for(self, submissions):
        open_subs = submissions._open_sub_batches()
        wizard = self.create({
            'submission_ids': [(6, 0, submissions.ids)],
            'allowed_component_ids': [(6, 0, submissions._open_batches(
            ).component_id.ids)],
            'has_open_sub_batch': bool(open_subs),
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _('Submit to the General Manager'),
            'res_model': self._name,
            'res_id': wizard.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_confirm(self):
        self.ensure_one()
        if self.mode == 'all':
            self.submission_ids.action_submit()
            return {'type': 'ir.actions.act_window_close'}
        if not (self.component_ids or self.sub_batch_ids):
            raise UserError(_(
                "Tick at least one component or sub-batch to submit."))
        # Sub-batches first: sending the last components can hand the whole
        # department over, after which there is nothing to send early.
        self.sub_batch_ids.action_submit()
        batches = self.submission_ids._open_batches().filtered(
            lambda b: b.component_id in self.component_ids)
        if batches:
            # As the supervisor, not the sudo() the lookup used: the hand-over
            # checks that these departments are his.
            batches.with_env(self.env).action_hand_over()
        return {'type': 'ir.actions.act_window_close'}


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
            runs._finalise_if_complete()
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
