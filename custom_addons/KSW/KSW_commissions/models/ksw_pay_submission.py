"""KSW Pay Submission — one department's handover to the General Manager.

The month is shared; the responsibility is not. A supervisor owns his own
department's entries and nobody else's, so the act of saying *"my department
is complete, please approve"* has to be a record of its own — one per scope,
per month.

Without it the design had a hole. The only submission was the run's, which
covers the whole company, so a supervisor could not declare himself done
without also declaring six other departments done. That is why the Monthly Pay
Run looked empty and unusable to him: the button on it was never his to press.

This model closes the hole and, in doing so, answers three things at once:

* **the supervisor gets a button that is only about his own records** — it
  submits his batches, and nothing else moves;
* **his batches stay reopenable until he presses it.** A submitted batch is
  just "I have finished typing this one"; the handover is what freezes it. So
  the two states finally mean different things, and the supervisor is no
  longer locked out by his own tidiness;
* **the GM sees who has handed over.** One row per department, with the
  supervisor's name, what it totals and when it arrived — which is exactly the
  list he needs before approving the month.

The scope of a submission mirrors the scope of the batches it collects
(department, work site, or company-wide), so every batch belongs to exactly
one submission and there is never a question of who owns what.
"""
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .ksw_commission_lock import LOCKING_STATES, check_period_unlocked

SUBMISSION_STATES = [
    ('draft', 'Being Prepared'),
    ('submitted', 'Submitted to GM'),
    ('returned', 'Returned for Correction'),
    ('approved', 'Approved'),
]


class KswPaySubmission(models.Model):
    _name = 'ksw.pay.submission'
    _description = 'KSW Department Commission Submission'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'period desc, department_id, site_id, id'

    run_id = fields.Many2one(
        'ksw.pay.run', required=True, ondelete='cascade', index=True,
        string='Month',
    )
    period = fields.Date(
        related='run_id.period', store=True, index=True, readonly=True,
    )
    department_id = fields.Many2one(
        'hr.department', ondelete='restrict', index=True, tracking=True,
    )
    site_id = fields.Many2one('ksw.site', ondelete='restrict', tracking=True)

    state = fields.Selection(
        SUBMISSION_STATES, default='draft', required=True, copy=False,
        tracking=True,
    )
    currency_id = fields.Many2one(
        related='run_id.currency_id', readonly=True,
    )

    batch_ids = fields.One2many(
        'ksw.pay.batch', 'submission_id', string='Batches',
    )

    # Who is answerable for this scope, whether or not they have submitted
    # yet — the department's manager. Shown to the GM next to the department
    # so "who has not handed over" is a name, not a puzzle.
    responsible_id = fields.Many2one(
        'res.users', compute='_compute_responsible', store=True,
        string='Supervisor',
    )

    # Who approves this department's handover. A different person from
    # `responsible_id` above: that is the DM who prepares the work, this is
    # the GM who signs it off. Conflating the two is exactly the confusion
    # the department GM exists to remove.
    gm_id = fields.Many2one(
        'res.users', compute='_compute_gm', store=True,
        string='General Manager',
    )
    submitted_by = fields.Many2one('res.users', readonly=True, copy=False)
    submitted_date = fields.Datetime(readonly=True, copy=False)
    returned_by = fields.Many2one('res.users', readonly=True, copy=False)
    return_reason = fields.Text(
        copy=False,
        help='Why the General Manager sent this back. Typed here, then '
             'Return — the supervisor sees it on every batch he has to fix.',
    )

    batch_count = fields.Integer(compute='_compute_totals', store=True)
    draft_batch_count = fields.Integer(compute='_compute_totals', store=True)
    entry_count = fields.Integer(compute='_compute_totals', store=True)
    employee_count = fields.Integer(compute='_compute_totals', store=True)
    total_amount = fields.Monetary(compute='_compute_totals', store=True)

    x_can_submit = fields.Boolean(compute='_compute_permissions')
    x_can_return = fields.Boolean(compute='_compute_permissions')
    x_can_approve = fields.Boolean(compute='_compute_permissions')

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends('department_id', 'department_id.manager_id')
    def _compute_responsible(self):
        for rec in self:
            manager = rec.department_id.sudo().manager_id
            rec.responsible_id = manager.user_id if manager else False

    @api.depends('department_id', 'department_id.x_effective_gm_id')
    def _compute_gm(self):
        """The GM answerable for this scope.

        A site-scoped submission (driver trips) has no department at all, so
        it falls back to the company default GM — same rule the leave and
        loan chains apply to employees with no department. ksw.pay.run is
        single-company and carries no company_id, so the environment's is the
        only one available.
        """
        default_gm = self.env.company.sudo().x_default_gm_id
        for rec in self:
            gm = rec.department_id.sudo().x_effective_gm_id or default_gm
            rec.gm_id = gm.sudo().user_id

    def _check_is_my_department(self):
        """Refuse unless the caller is this scope's General Manager.

        The record rule filters what a GM sees; this is what stops the RPC.
        No blanket override for the sitting GM: if he is not named on the
        department, it is not his to approve.
        """
        if self.env.su:
            return
        user = self.env.user
        for rec in self:
            if rec.gm_id and rec.gm_id == user:
                continue
            if rec.gm_id:
                raise UserError(_(
                    "Only %(gm)s, the General Manager of %(scope)s, can act "
                    "on this submission.",
                    gm=rec.gm_id.name, scope=rec.display_name))
            raise UserError(_(
                "No General Manager is set for %(scope)s. Ask HR to set the "
                "department's General Manager.", scope=rec.display_name))

    @api.depends('batch_ids.state', 'batch_ids.total_amount',
                 'batch_ids.entry_count', 'batch_ids.employee_count',
                 'batch_ids.entry_ids.employee_id')
    def _compute_totals(self):
        for rec in self:
            batches = rec.batch_ids
            rec.batch_count = len(batches)
            rec.draft_batch_count = len(
                batches.filtered(lambda b: b.state == 'draft'))
            rec.entry_count = sum(batches.mapped('entry_count'))
            rec.employee_count = len(
                batches.mapped('entry_ids.employee_id'))
            rec.total_amount = sum(batches.mapped('total_amount'))

    @api.depends_context('uid')
    @api.depends('state', 'run_id.state', 'gm_id')
    def _compute_permissions(self):
        user = self.env.user
        for rec in self:
            locked = rec.run_id.state in LOCKING_STATES
            # Per record, not per user: being a GM somewhere says nothing
            # about whether this department is his.
            is_my_gm = bool(rec.gm_id) and rec.gm_id == user
            rec.x_can_submit = (
                not locked
                and rec.state in ('draft', 'returned')
                and bool(rec.batch_ids)
            )
            rec.x_can_return = is_my_gm and not locked \
                and rec.state == 'submitted'
            rec.x_can_approve = is_my_gm and not locked \
                and rec.state == 'submitted'

    @api.depends('department_id', 'site_id', 'period')
    def _compute_display_name(self):
        for rec in self:
            scope = rec.department_id.name or rec.site_id.name or _('Company')
            period = rec.period.strftime('%b %Y') if rec.period else ''
            rec.display_name = '%s · %s' % (scope, period)

    # ------------------------------------------------------------------
    # Find-or-create — a submission exists as soon as its first batch does
    # ------------------------------------------------------------------
    @api.model
    def _for_scope(self, period, department=None, site=None):
        """The submission a batch of this scope belongs to, creating the
        month and the submission if this is the first batch to need them."""
        Run = self.env['ksw.pay.run'].sudo()
        period = fields.Date.to_date(period).replace(day=1)
        run = Run.search([('period', '=', period)], limit=1)
        if not run:
            run = Run.create({'period': period})
        domain = [
            ('run_id', '=', run.id),
            ('department_id', '=', department.id if department else False),
            ('site_id', '=', site.id if site else False),
        ]
        submission = self.sudo().search(domain, limit=1)
        if not submission:
            submission = self.sudo().create({
                'run_id': run.id,
                'department_id': department.id if department else False,
                'site_id': site.id if site else False,
            })
        return submission

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def write(self, vals):
        if not self.env.su:
            protected = set(vals) - {
                'state', 'return_reason', 'returned_by', 'submitted_by',
                'submitted_date', 'message_follower_ids', 'message_ids',
                'activity_ids', 'message_main_attachment_id',
            }
            for rec in self:
                if rec.state == 'approved' and protected:
                    raise UserError(_(
                        "%(name)s has been approved and can no longer be "
                        "changed.", name=rec.display_name))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            blocked = self.filtered(lambda s: s.state != 'draft')
            if blocked:
                raise UserError(_(
                    "Only a submission still being prepared can be deleted: "
                    "%(names)s",
                    names=', '.join(blocked.mapped('display_name'))))
        return super().unlink()

    # ------------------------------------------------------------------
    # Authorisation
    # ------------------------------------------------------------------
    def _check_mine(self):
        """Refuse to let one supervisor hand over another's work.

        The record rule already hides other departments, but a rule is a
        filter, not a guard — this is what stops the RPC call.
        """
        if self.env.su:
            return
        if self.env.user.has_group('KSW_commissions.group_commission_officer'):
            return
        allowed = self.env['ksw.pay.batch']._allowed_departments()
        for rec in self:
            if rec.department_id and rec.department_id in allowed:
                continue
            if rec.batch_ids.filtered(
                    lambda b: b.create_uid == self.env.user):
                continue
            raise UserError(_(
                "%(name)s is not yours to submit.", name=rec.display_name))

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def action_submit(self):
        """Hand this department's month over to the General Manager.

        Submits every batch still in draft on the way through, so the
        supervisor has one button to press rather than one per batch — and
        from this moment his batches are frozen until the GM returns them.
        """
        self._check_mine()
        for rec in self:
            check_period_unlocked(
                self.env, rec.period, _("Submitting this department"))
            if rec.state == 'approved':
                raise UserError(_(
                    "%(name)s has already been approved.",
                    name=rec.display_name))
            if rec.state == 'submitted':
                raise UserError(_(
                    "%(name)s has already been submitted.",
                    name=rec.display_name))
            if not rec.batch_ids:
                raise UserError(_(
                    "There is nothing to submit — no entries have been "
                    "recorded for %(name)s.", name=rec.display_name))
            empty = rec.batch_ids.filtered(lambda b: not b.entry_ids)
            if empty:
                raise UserError(_(
                    "These batches have no entries. Delete them or fill them "
                    "in before submitting:\n%(names)s",
                    names='\n'.join('  • %s' % b.name for b in empty)))

            drafts = rec.batch_ids.filtered(lambda b: b.state == 'draft')
            if drafts:
                drafts.action_submit()

            rec.write({
                'state': 'submitted',
                'submitted_by': self.env.uid,
                'submitted_date': fields.Datetime.now(),
                'return_reason': False,
            })
            rec._notify_gm()
        self.mapped('run_id')._sync_state()
        self.mapped('run_id')._refresh_register()
        return True

    def action_approve(self):
        """The GM signs off his own department, on its own.

        Approval used to be one atomic act on the month: every submitted
        department at once, then build the register and lock. With a GM per
        department that is nobody's decision to make — so each department is
        approved by its own GM here, and closing the month becomes a separate
        act (see ksw.pay.run.action_approve / action_close_month).
        """
        self._check_is_my_department()
        for rec in self:
            check_period_unlocked(
                self.env, rec.period, _("Approving this department"))
            if rec.state == 'approved':
                raise UserError(_(
                    "%(name)s has already been approved.",
                    name=rec.display_name))
            if rec.state != 'submitted':
                raise UserError(_(
                    "%(name)s has not been submitted yet, so there is "
                    "nothing to approve.", name=rec.display_name))
            rec.batch_ids.filtered(
                lambda b: b.state == 'submitted').sudo().write(
                    {'state': 'approved'})
            rec.sudo().write({'state': 'approved'})
            # sudo(): mail.message create is gated on access to the document,
            # and a GM's scope here is one department. Authorisation was
            # settled by _check_is_my_department above.
            rec.sudo().message_post(
                body=Markup(
                    '<strong>✅ Approved</strong><br/>'
                    '<b>Department:</b> %(scope)s<br/>'
                    '<b>By:</b> %(user)s<br/>'
                    '<b>Employees:</b> %(emp)s · <b>Total:</b> %(total).2f'
                ) % {'scope': rec.display_name, 'user': self.env.user.name,
                     'emp': rec.employee_count,
                     'total': rec.total_amount or 0.0},
                subtype_xmlid='mail.mt_note',
            )
        self.mapped('run_id')._sync_state()
        self.mapped('run_id')._refresh_register()
        return True

    def action_return(self):
        """GM sends the department back, with the reason on every batch."""
        self._check_is_my_department()
        for rec in self:
            if rec.state != 'submitted':
                raise UserError(_(
                    "Only a submitted department can be returned."))
            reason = (rec.return_reason or '').strip()
            if not reason:
                raise UserError(_(
                    "Say why it is being returned — the supervisor needs to "
                    "know what to fix. Type it in 'Return Reason' first."))
            rec.batch_ids.filtered(
                lambda b: b.state == 'submitted').action_return(reason)
            rec.write({
                'state': 'returned',
                'returned_by': self.env.uid,
                'submitted_by': False,
                'submitted_date': False,
            })
            partners = rec._supervisor_partners()
            rec.sudo().message_post(
                body=Markup(
                    '<strong>↩ Returned for correction</strong><br/>'
                    '<b>By:</b> %(user)s<br/><b>Reason:</b> %(reason)s'
                ) % {'user': self.env.user.name, 'reason': reason},
                partner_ids=partners.ids,
                subtype_xmlid='mail.mt_comment',
            )
        self.mapped('run_id')._sync_state()
        self.mapped('run_id')._refresh_register()
        return True

    def action_reset_to_draft(self):
        """Take a submission back before the GM has acted on it."""
        self._check_mine()
        for rec in self:
            check_period_unlocked(
                self.env, rec.period, _("Reopening this department"))
            if rec.state == 'approved':
                raise UserError(_(
                    "%(name)s has been approved. Only the General Manager "
                    "can reopen the month.", name=rec.display_name))
            rec.write({'state': 'draft', 'submitted_by': False,
                       'submitted_date': False})
        self.mapped('run_id')._sync_state()
        self.mapped('run_id')._refresh_register()
        return True

    # ------------------------------------------------------------------
    # Notification
    # ------------------------------------------------------------------
    def _gm_partners(self):
        """This scope's GM — one person, not the whole GM group.

        Mailing the group meant every GM was told about every department's
        handover, which is the broadcast the department GM replaces.
        """
        return self.mapped('gm_id').partner_id

    def _supervisor_partners(self):
        self.ensure_one()
        users = self.submitted_by | self.responsible_id \
            | self.batch_ids.mapped('create_uid')
        return users.partner_id

    def _notify_gm(self):
        """Tell the GM this department is in — in his inbox and by email.

        Both on purpose: the inbox message is the audit trail on the record,
        the email is what reaches him when he is not in Odoo.
        """
        self.ensure_one()
        partners = self._gm_partners()
        self.sudo().message_post(
            body=Markup(
                '<strong>📤 Submitted for approval</strong><br/>'
                '<b>Department:</b> %(scope)s<br/>'
                '<b>By:</b> %(user)s<br/>'
                '<b>Batches:</b> %(batches)s · '
                '<b>Employees:</b> %(emp)s<br/>'
                '<b>Total:</b> %(total).2f'
            ) % {'scope': self.display_name, 'user': self.env.user.name,
                 'batches': self.batch_count, 'emp': self.employee_count,
                 'total': self.total_amount or 0.0},
            partner_ids=partners.ids,
            subtype_xmlid='mail.mt_comment',
        )
        if not partners:
            return
        template = self.env.ref(
            'KSW_commissions.mail_template_submission_to_gm',
            raise_if_not_found=False)
        if not template:
            return
        template.sudo().send_mail(
            self.id, force_send=False,
            email_values={'recipient_ids': [(6, 0, partners.ids)]},
        )

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def action_open_batches(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Batches — %(name)s', name=self.display_name),
            'res_model': 'ksw.pay.batch',
            'view_mode': 'list,form',
            'domain': [('submission_id', '=', self.id)],
        }

    def action_open_entries(self):
        """Every entry in this department, grouped per employee.

        This is the detail behind the department's total: who is being paid,
        for what, and how much each of them adds up to.
        """
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Entries — %(name)s', name=self.display_name),
            'res_model': 'ksw.pay.entry',
            'view_mode': 'list,form',
            'domain': [('batch_id.submission_id', '=', self.id)],
            'context': {'search_default_grp_employee': 1},
        }
