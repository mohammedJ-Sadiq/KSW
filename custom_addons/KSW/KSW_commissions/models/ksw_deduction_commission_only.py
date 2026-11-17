"""Settle a deduction from commission only, never from the payslip.

Accounting had been doing this by hand: KSWCO PE00067 (15,947 SAR) had its
installment rescheduled 15 months ahead so payroll would never reach it,
then manual payments were recorded whenever a commission was paid — and
because of that date the approved September commission took nothing.

Now a status on the deduction, ``x_commission_only``, that only a General
Manager's decision can turn on:

* asked for by Loan Modification = Full (``group_installment_edit``), the
  Deductions Administrator, or the Accounting Data Entry user who created
  the (non-loan) deduction — always with a reason;
* decided by the employee's department GM (``_department_gm_user``) on the
  request itself, its one home (Home ▸ Waiting for you); the deduction only
  shows where it waits;
* switched back to payroll by the same requesters without the GM — that
  restores the default rule — but logged the same way and the GM is told.

Every request, decided or not, is a ``ksw.deduction.commission.only``
record: who asked, why, the balance and the recent commission at that
moment, who decided, when, and the note. Nothing in it is editable after
the decision.

What the status changes is two predicates and nothing else:

* payroll — ``hr.payslip._ksw_pending_lines_domain`` leaves the deduction
  out of ordinary monthly payslips. A vacation / EOS settlement still
  collects it (``full=True``): it is the last payslip that carries the
  employee's commission, and the debt must not leave with him;
* commission — ``ksw.deduction._get_pending_commission_lines_for_period``
  hands every due-or-overdue installment to each approved commission month,
  oldest first; what the month cannot cover simply stays due for the next.
"""
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Domain

#: Context key the request uses to write the status — the only writer.
_DECISION_CTX = '_ksw_commission_only_decision'

#: Deduction states the status can be asked for or decided on.
_OPEN_STATES = ('draft', 'active')


class KswDeductionCommissionOnly(models.Model):
    _name = 'ksw.deduction.commission.only'
    _description = 'Commission-Only Settlement Request'
    _inherit = ['mail.thread']
    _order = 'requested_on desc, id desc'
    _rec_name = 'name'

    name = fields.Char(readonly=True, copy=False, default='/')
    deduction_id = fields.Many2one(
        'ksw.deduction', string='Deduction', required=True, readonly=True,
        index=True, ondelete='restrict')
    # Stored so the GM's own counters never join through ksw.deduction,
    # whose record rules he may not pass (non-loan deductions).
    deduction_state = fields.Selection(
        related='deduction_id.state', store=True, string='Deduction Status')
    employee_id = fields.Many2one(
        related='deduction_id.employee_id', store=True, string='Employee')
    type_id = fields.Many2one(
        related='deduction_id.type_id', store=True, string='Type')
    managed_by = fields.Selection(
        related='deduction_id.managed_by', store=True)
    currency_id = fields.Many2one(related='deduction_id.currency_id')
    action = fields.Selection([
        ('enable', 'Settle from commission only'),
        ('disable', 'Back to payroll deduction'),
    ], required=True, readonly=True, default='enable')
    state = fields.Selection([
        ('pending_gm', 'Waiting for GM'),
        ('approved', 'Approved'),
        ('refused', 'Refused'),
        ('withdrawn', 'Withdrawn'),
        ('done', 'Recorded'),
    ], required=True, readonly=True, default='pending_gm', tracking=True)
    reason = fields.Text(required=True, readonly=True)
    requested_by = fields.Many2one(
        'res.users', required=True, readonly=True,
        default=lambda self: self.env.user)
    requested_on = fields.Datetime(
        required=True, readonly=True, default=fields.Datetime.now)
    gm_user_id = fields.Many2one(
        'res.users', string='General Manager', readonly=True, index=True,
        help='The department General Manager who decides this request, '
             'fixed when it was made.')
    decided_by = fields.Many2one('res.users', readonly=True)
    decided_on = fields.Datetime(readonly=True)
    decision_note = fields.Text(
        help='Required to refuse. Written by the General Manager.')

    # Snapshot of what the decision was taken on — plain fields, filled at
    # creation, so the record keeps saying what the GM saw.
    outstanding_at_request = fields.Monetary(
        string='Outstanding at Request', readonly=True,
        currency_field='currency_id')
    commission_history = fields.Html(
        string='Recent Commission', readonly=True, sanitize=True)

    x_is_pending_my_action = fields.Boolean(
        compute='_compute_is_pending_my_action',
        search='_search_is_pending_my_action',
        string='Waiting for Me')
    x_can_withdraw = fields.Boolean(
        string='Can Withdraw', compute='_compute_is_pending_my_action')

    @api.model
    def _pending_domain(self):
        return Domain([
            ('state', '=', 'pending_gm'),
            ('gm_user_id', '=', self.env.uid),
            ('deduction_state', 'in', list(_OPEN_STATES)),
        ])

    @api.depends_context('uid')
    @api.depends('state', 'gm_user_id', 'deduction_state', 'requested_by')
    def _compute_is_pending_my_action(self):
        uid = self.env.uid
        for rec in self:
            rec.x_is_pending_my_action = (
                rec.state == 'pending_gm' and rec.gm_user_id.id == uid
                and rec.deduction_state in _OPEN_STATES)
            rec.x_can_withdraw = (rec.state == 'pending_gm'
                                  and rec.requested_by.id == uid)

    def _search_is_pending_my_action(self, operator, value):
        if operator in ('in', 'not in'):
            positive = (operator == 'in') == any(value)
        elif operator in ('=', '!='):
            positive = (operator == '=') == bool(value)
        else:
            return NotImplemented
        domain = self._pending_domain()
        return domain if positive else ~domain

    @api.model_create_multi
    def create(self, vals_list):
        Seq = self.env['ir.sequence'].sudo()
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = Seq.next_by_code(
                    'ksw.deduction.commission.only') or '/'
        return super().create(vals_list)

    def write(self, vals):
        # Everything but the GM's note is written by the methods below,
        # under sudo after their own checks; a decided request never moves.
        if not self.env.su:
            if set(vals) - {'decision_note'}:
                raise UserError(_(
                    'A commission-only request is changed only through its '
                    'buttons.'))
            for rec in self:
                if rec.state != 'pending_gm' or rec.gm_user_id != self.env.user:
                    raise UserError(_(
                        'Only %(gm)s can write the decision note, and only '
                        'while the request is waiting.',
                        gm=rec.gm_user_id.name or _('the General Manager')))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise UserError(_(
                'Commission-only requests are a permanent record and cannot '
                'be deleted.'))
        return super().unlink()

    # ------------------------------------------------------------------
    # Decision
    # ------------------------------------------------------------------
    def _check_gm(self):
        if self.env.su:
            return
        for rec in self:
            if rec.gm_user_id != self.env.user:
                raise UserError(_(
                    'Only %(gm)s, the General Manager of this employee\'s '
                    'department, can decide this request.',
                    gm=rec.gm_user_id.name or _('the General Manager')))
            if rec.requested_by == self.env.user:
                raise UserError(_(
                    'You asked for this yourself, so you cannot also approve '
                    'it.'))

    def _check_waiting(self):
        for rec in self:
            if rec.state != 'pending_gm':
                raise UserError(_('%(name)s is no longer waiting for a '
                                  'decision.', name=rec.name))
            if rec.deduction_state not in _OPEN_STATES:
                raise UserError(_(
                    '%(ded)s is %(state)s, so there is nothing left to '
                    'decide.', ded=rec.deduction_id.sudo().name,
                    state=dict(rec._fields['deduction_state']
                               ._description_selection(self.env)).get(
                                   rec.deduction_state)))

    def action_gm_approve(self):
        self._check_gm()
        self._check_waiting()
        for rec in self.sudo():
            ded = rec.deduction_id
            rec.write({'state': 'approved', 'decided_by': self.env.uid,
                       'decided_on': fields.Datetime.now()})
            ded._set_commission_only(True, rec)
            rec._post_decision(_('Approved by the General Manager'))
        return True

    def action_gm_refuse(self):
        self._check_gm()
        self._check_waiting()
        for rec in self:
            if not (rec.decision_note or '').strip():
                raise UserError(_(
                    'Write the reason for refusing in the Decision Note '
                    'first.'))
        for rec in self.sudo():
            rec.write({'state': 'refused', 'decided_by': self.env.uid,
                       'decided_on': fields.Datetime.now()})
            rec._post_decision(_('Refused by the General Manager'))
        return True

    def action_withdraw(self):
        for rec in self:
            if not self.env.su and rec.requested_by != self.env.user:
                raise UserError(_('Only %(user)s, who asked, can withdraw '
                                  'it.', user=rec.requested_by.name))
            if rec.state != 'pending_gm':
                raise UserError(_('%(name)s is no longer waiting.',
                                  name=rec.name))
        for rec in self.sudo():
            rec.write({'state': 'withdrawn', 'decided_by': self.env.uid,
                       'decided_on': fields.Datetime.now()})
            rec._post_decision(_('Withdrawn by the requester'))
        return True

    def _post_decision(self, title):
        """The decision on the request (requester notified) and on the
        deduction, where everyone reading it can see it."""
        self.ensure_one()
        body = Markup(
            '<strong>%(title)s</strong><br/>'
            '<b>%(l_req)s</b> %(req)s<br/>'
            '<b>%(l_by)s</b> %(by)s<br/>'
        ) % {
            'title': title, 'l_req': _('Request:'), 'req': self.name,
            'l_by': _('By:'), 'by': self.env.user.name,
        }
        if self.decision_note:
            body += Markup('<b>%(l)s</b> %(n)s') % {
                'l': _('Note:'), 'n': self.decision_note}
        partners = self.requested_by.partner_id
        self.message_post(body=body, partner_ids=partners.ids,
                          subtype_xmlid='mail.mt_comment')
        self.deduction_id.message_post(body=body, subtype_xmlid='mail.mt_note')

    def action_open_deduction(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'ksw.deduction',
            'res_id': self.deduction_id.id,
            'view_mode': 'form',
            'target': 'current',
        }


class KswDeduction(models.Model):
    _inherit = 'ksw.deduction'

    x_commission_only = fields.Boolean(
        string='Settle from Commission Only', readonly=True, copy=False,
        tracking=True,
        help='Approved by the General Manager: ordinary payslips never '
             'deduct this. Each approved commission month takes the due and '
             'overdue installments; a vacation or end-of-service settlement '
             'still collects it.')
    x_commission_only_request_id = fields.Many2one(
        'ksw.deduction.commission.only', string='Commission-Only Decision',
        readonly=True, copy=False,
        help='The request whose decision set the current status.')
    # Never put on a view: the deduction's readers (HR approvers,
    # supervisors) cannot read the requests, and an o2m they cannot read
    # fails the whole form. The sudo computes below are what views show.
    x_commission_only_request_ids = fields.One2many(
        'ksw.deduction.commission.only', 'deduction_id',
        string='Commission-Only Requests', readonly=True)
    x_commission_only_pending_id = fields.Many2one(
        'ksw.deduction.commission.only', string='Waiting Request',
        compute='_compute_commission_only_info', compute_sudo=True)
    x_commission_only_pending_gm = fields.Char(
        string='Waiting for General Manager',
        compute='_compute_commission_only_info', compute_sudo=True)
    x_commission_only_log_count = fields.Integer(
        string='Commission-Only Log Count',
        compute='_compute_commission_only_info', compute_sudo=True)
    x_can_request_commission_only = fields.Boolean(
        string='Can Ask for Commission Only', compute='_compute_can_commission_only')
    x_can_end_commission_only = fields.Boolean(
        string='Can Switch Back to Payroll', compute='_compute_can_commission_only')

    @api.depends('x_commission_only_request_ids.state',
                 'x_commission_only_request_ids.gm_user_id')
    def _compute_commission_only_info(self):
        for rec in self:
            requests = rec.x_commission_only_request_ids
            pending = requests.filtered(lambda r: r.state == 'pending_gm')[:1]
            rec.x_commission_only_pending_id = pending
            rec.x_commission_only_pending_gm = pending.gm_user_id.name or ''
            rec.x_commission_only_log_count = len(requests)

    @api.depends_context('uid')
    @api.depends('state', 'x_commission_only', 'managed_by', 'create_uid',
                 'x_commission_only_request_ids.state')
    def _compute_can_commission_only(self):
        for rec in self:
            allowed = rec.state in _OPEN_STATES and rec._may_ask_commission_only()
            rec.x_can_request_commission_only = (
                allowed and not rec.x_commission_only
                and not rec.x_commission_only_pending_id)
            rec.x_can_end_commission_only = allowed and rec.x_commission_only

    def _may_ask_commission_only(self):
        """Loan Modification = Full and the administrator, on any type; the
        Accounting Data Entry user on a non-loan deduction he created."""
        self.ensure_one()
        user = self.env.user
        if (user.has_group('KSW_deduction.group_installment_edit')
                or user.has_group('KSW_deduction.group_deduction_manager')):
            return True
        return (user.has_group('KSW_deduction.group_acc_data_entry')
                and self.managed_by == 'acc_data_entry'
                and self.create_uid == user)

    def _check_may_ask_commission_only(self):
        if self.env.su:
            return
        for rec in self:
            if not rec._may_ask_commission_only():
                raise UserError(_(
                    'Only accounting with Loan Modification = Full, or the '
                    'Accounting Data Entry user who created %(ded)s, can '
                    'change how it is settled.', ded=rec.name))
            if rec.state not in _OPEN_STATES:
                raise UserError(_('%(ded)s is closed.', ded=rec.name))

    def write(self, vals):
        if ('x_commission_only' in vals or 'x_commission_only_request_id'
                in vals) and not self.env.context.get(_DECISION_CTX):
            raise UserError(_(
                'Settling from commission only is set by a General Manager\'s '
                'decision, not edited directly.'))
        return super().write(vals)

    # ------------------------------------------------------------------
    # Request / switch back
    # ------------------------------------------------------------------
    def _commission_history_html(self, months=3):
        """The employee's last approved commission months, for the GM."""
        self.ensure_one()
        lines = self.env['ksw.pay.run.line'].sudo().search([
            ('employee_id', '=', self.employee_id.id),
            ('run_id.state', 'in', ('approved', 'paid')),
        ], order='period desc', limit=months)
        if not lines:
            return Markup('<p><b>%s</b></p>') % _(
                'No approved commission in any month: nothing would collect '
                'this.')
        body = Markup('<ul>')
        for line in lines:
            body += Markup('<li>%(month)s: %(amt).2f</li>') % {
                'month': line.period.strftime('%m/%Y'),
                'amt': line.earnings or 0.0}
        return body + Markup('</ul>')

    def _request_commission_only(self, reason):
        self.ensure_one()
        self._check_may_ask_commission_only()
        if self.x_commission_only:
            raise UserError(_('%(ded)s is already settled from commission '
                              'only.', ded=self.name))
        if self.x_commission_only_pending_id:
            raise UserError(_('%(ded)s already has a request waiting for the '
                              'General Manager.', ded=self.name))
        ded = self.sudo()
        gm = ded._department_gm_user()
        if not gm:
            raise UserError(_(
                'No General Manager is set for %(dept)s, so nobody can '
                'decide this. Ask HR to set the department\'s General '
                'Manager.', dept=ded.employee_id.department_id.display_name
                or _('this employee\'s department')))
        request = self.env['ksw.deduction.commission.only'].sudo().create({
            'deduction_id': ded.id,
            'action': 'enable',
            'reason': reason,
            'requested_by': self.env.uid,
            'gm_user_id': gm.id,
            'outstanding_at_request': ded.total_pending,
            'commission_history': ded._commission_history_html(),
        })
        body = Markup(
            '<strong>%(title)s</strong><br/>'
            '<b>%(l_ded)s</b> %(ded)s — %(emp)s<br/>'
            '<b>%(l_out)s</b> %(out).2f<br/>'
            '<b>%(l_why)s</b> %(why)s'
        ) % {
            'title': _('Settle from commission only: your decision'),
            'l_ded': _('Deduction:'), 'ded': ded.name,
            'emp': ded.employee_id.name,
            'l_out': _('Outstanding:'), 'out': ded.total_pending,
            'l_why': _('Reason:'), 'why': reason,
        }
        request.message_post(body=body, partner_ids=gm.partner_id.ids,
                             subtype_xmlid='mail.mt_comment')
        ded.message_post(body=Markup(
            '<strong>%(title)s</strong><br/><b>%(l)s</b> %(gm)s<br/>'
            '<b>%(l_why)s</b> %(why)s<br/><i>%(note)s</i>') % {
                'title': _('Asked to settle from commission only (%s)')
                % request.name,
                'l': _('Waiting for:'), 'gm': gm.name,
                'l_why': _('Reason:'), 'why': reason,
                'note': _('Payroll keeps deducting it until the General '
                          'Manager approves.'),
            }, subtype_xmlid='mail.mt_note')
        return request

    def _end_commission_only(self, reason):
        """Back to the payslip: the default rule, so no GM step — but the
        same log, and the GM is told."""
        self.ensure_one()
        self._check_may_ask_commission_only()
        if not self.x_commission_only:
            raise UserError(_('%(ded)s is not settled from commission only.',
                              ded=self.name))
        ded = self.sudo()
        gm = ded._department_gm_user()
        request = self.env['ksw.deduction.commission.only'].sudo().create({
            'deduction_id': ded.id,
            'action': 'disable',
            'state': 'done',
            'reason': reason,
            'requested_by': self.env.uid,
            'gm_user_id': gm.id,
            'decided_by': self.env.uid,
            'decided_on': fields.Datetime.now(),
            'outstanding_at_request': ded.total_pending,
            'commission_history': ded._commission_history_html(),
        })
        ded._set_commission_only(False, request)
        body = Markup(
            '<strong>%(title)s</strong><br/>'
            '<b>%(l_req)s</b> %(req)s<br/><b>%(l_by)s</b> %(by)s<br/>'
            '<b>%(l_why)s</b> %(why)s'
        ) % {
            'title': _('Back to payroll deduction'),
            'l_req': _('Request:'), 'req': request.name,
            'l_by': _('By:'), 'by': self.env.user.name,
            'l_why': _('Reason:'), 'why': reason,
        }
        request.message_post(body=body, partner_ids=gm.partner_id.ids,
                             subtype_xmlid='mail.mt_comment')
        ded.message_post(body=body, subtype_xmlid='mail.mt_note')
        return request

    def _set_commission_only(self, value, request):
        """The one writer of the status, then everything that reads it."""
        self.ensure_one()
        ded = self.sudo()
        ded.with_context(**{_DECISION_CTX: True}).write({
            'x_commission_only': value,
            'x_commission_only_request_id': request.id,
        })
        pending = ded.line_ids.filtered(lambda l: l.state == 'pending')
        # The open month's Loans column, wherever this employee sits.
        keys = pending._pay_run_estimate_keys()
        open_lines = self.env['ksw.pay.run.line'].sudo().search([
            ('employee_id', '=', ded.employee_id.id),
            ('run_id.state', 'not in', ('approved', 'paid')),
        ])
        keys |= {(l.employee_id.id, l.period.year, l.period.month)
                 for l in open_lines}
        pending._refresh_pay_run_estimates(keys)
        ded._commission_only_refresh_payslips(value)
        if value:
            # An approved, not-Paid month can collect it now (non-loan rule).
            ded._recover_from_unpaid_commission()

    def _commission_only_refresh_payslips(self, value):
        """Draft payslips computed before the change drop (or pick up) the
        installments now; a done payslip is paid and never restated."""
        self.ensure_one()
        pending = self.line_ids.filtered(lambda l: l.state == 'pending')
        if not pending:
            return
        Slip = self.env['hr.payslip'].sudo()
        slips = Slip.search([
            ('employee_id', '=', self.employee_id.id),
            ('state', '=', 'draft'),
            ('x_is_revision', '=', False),
            ('date_to', '>=', min(pending.mapped('period_date'))),
        ])
        for slip in slips:
            before = slip._ksw_net_amount()
            computed = bool(slip.line_ids)
            try:
                with self.env.cr.savepoint():
                    slip.compute_sheet()
            except Exception:  # noqa: BLE001 - never block the decision
                self.env.invalidate_all()
                continue
            after = slip._ksw_net_amount()
            if computed and round(after - before, 2):
                slip.message_post(body=Markup(
                    '<strong>%(title)s</strong><br/>'
                    '<b>NET:</b> %(before).2f &rarr; %(after).2f<br/>%(hint)s'
                ) % {
                    'title': (_('%s is now settled from commission only')
                              if value else
                              _('%s is deducted from the payslip again'))
                    % self.name,
                    'before': before, 'after': after,
                    'hint': _('Re-export the bank file if it was already '
                              'generated for this batch.'),
                }, subtype_xmlid='mail.mt_note')

    def action_open_commission_only_wizard(self):
        self.ensure_one()
        self._check_may_ask_commission_only()
        return {
            'type': 'ir.actions.act_window',
            'name': (_('Back to Payroll Deduction') if self.x_commission_only
                     else _('Settle from Commission Only')),
            'res_model': 'ksw.deduction.commission.only.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_deduction_id': self.id,
                'default_action': 'disable' if self.x_commission_only
                else 'enable',
            },
        }

    def action_open_commission_only_pending(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'ksw.deduction.commission.only',
            'res_id': self.x_commission_only_pending_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_view_commission_only_log(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'KSW_commissions.action_ksw_deduction_commission_only')
        action['domain'] = [('deduction_id', '=', self.id)]
        action['context'] = {'create': False}
        return action


class KswDeductionCommissionOnlyWizard(models.TransientModel):
    _name = 'ksw.deduction.commission.only.wizard'
    _description = 'Ask to Settle a Deduction from Commission Only'

    deduction_id = fields.Many2one('ksw.deduction', required=True,
                                   readonly=True)
    action = fields.Selection([
        ('enable', 'Settle from commission only'),
        ('disable', 'Back to payroll deduction'),
    ], required=True, readonly=True)
    currency_id = fields.Many2one(related='deduction_id.currency_id')
    outstanding = fields.Monetary(
        compute='_compute_info', currency_field='currency_id')
    commission_history = fields.Html(compute='_compute_info')
    gm_name = fields.Char(compute='_compute_info', string='Decided by')
    reason = fields.Text(required=True)

    @api.depends('deduction_id')
    def _compute_info(self):
        for wiz in self:
            ded = wiz.deduction_id.sudo()
            wiz.outstanding = ded.total_pending
            wiz.commission_history = (ded._commission_history_html()
                                      if ded else False)
            wiz.gm_name = ded._department_gm_user().name if ded else ''

    def action_confirm(self):
        self.ensure_one()
        reason = (self.reason or '').strip()
        if not reason:
            raise UserError(_('Give the reason.'))
        if self.action == 'enable':
            self.deduction_id._request_commission_only(reason)
        else:
            self.deduction_id._end_commission_only(reason)
        return {'type': 'ir.actions.act_window_close'}
