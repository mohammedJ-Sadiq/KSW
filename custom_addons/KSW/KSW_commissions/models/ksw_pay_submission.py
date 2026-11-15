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

from .ksw_commission_lock import (
    HANDED_STATES, LOCKING_STATES, check_period_unlocked, gm_for_department)

SUBMISSION_STATES = [
    ('draft', 'Being Prepared'),
    ('checking', 'With Accounting'),
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
    sub_batch_ids = fields.One2many(
        'ksw.pay.sub.batch', 'submission_id', string='Sub-Batches',
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

    # What is actually in front of the GM: rows of a department handed over
    # as a whole, plus rows sent early in a sub-batch. Not the department's
    # state — a department still being prepared can have a sub-batch
    # waiting, and a returned one can still have rows to approve.
    pending_entry_ids = fields.Many2many(
        'ksw.pay.entry', compute='_compute_pending',
        string='Waiting on the GM')
    pending_entry_count = fields.Integer(compute='_compute_pending')
    pending_employee_count = fields.Integer(compute='_compute_pending')
    pending_amount = fields.Monetary(compute='_compute_pending')
    approved_entry_count = fields.Integer(compute='_compute_pending')
    sub_batch_count = fields.Integer(compute='_compute_pending')
    # Accounting's check, before the GM (Nov 2026): handed-over rows the
    # department's accountants have not checked yet. The pending_* fields
    # above are the GM's — rows they have.
    acc_entry_ids = fields.Many2many(
        'ksw.pay.entry', compute='_compute_pending',
        string='With Accounting')
    acc_entry_count = fields.Integer(
        compute='_compute_pending', string='Entries with Accounting')
    acc_amount = fields.Monetary(
        compute='_compute_pending', string='Amount with Accounting')
    x_can_acc_check = fields.Boolean(compute='_compute_permissions')
    x_is_pending_my_action = fields.Boolean(
        compute='_compute_permissions', search='_search_is_pending_my_action',
        string='Waiting for Me')

    x_can_submit = fields.Boolean(compute='_compute_permissions')
    x_can_return = fields.Boolean(compute='_compute_permissions')
    x_can_approve = fields.Boolean(compute='_compute_permissions')
    x_can_reopen_rows = fields.Boolean(compute='_compute_permissions')
    x_can_split = fields.Boolean(compute='_compute_permissions')

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

    @api.depends('state', 'batch_ids.entry_ids.state',
                 'batch_ids.entry_ids.amount',
                 'batch_ids.entry_ids.x_acc_checked_by',
                 'batch_ids.entry_ids.x_sub_batch_id', 'sub_batch_ids.state')
    def _compute_pending(self):
        for rec in self:
            entries = rec.sudo().batch_ids.entry_ids
            checking = entries._awaiting_accounting()
            rec.acc_entry_ids = checking
            rec.acc_entry_count = len(checking)
            rec.acc_amount = sum(checking.mapped('amount'))
            pending = entries._awaiting_gm()
            rec.pending_entry_ids = pending
            rec.pending_entry_count = len(pending)
            rec.pending_employee_count = len(pending.employee_id)
            rec.pending_amount = sum(pending.mapped('amount'))
            rec.approved_entry_count = len(
                entries.filtered(lambda e: e.state == 'approved'))
            rec.sub_batch_count = len(rec.sudo().sub_batch_ids)

    @api.depends_context('uid')
    @api.depends('state', 'run_id.state', 'gm_id', 'pending_entry_count',
                 'approved_entry_count', 'acc_entry_count',
                 'department_id.x_effective_accountant_ids')
    def _compute_permissions(self):
        user = self.env.user
        is_supervisor = user.has_group(
            'KSW_commissions.group_commission_supervisor')
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
            waiting = bool(rec.pending_entry_count)
            rec.x_can_return = is_my_gm and not locked and waiting
            rec.x_can_approve = is_my_gm and not locked and waiting
            rec.x_can_reopen_rows = is_my_gm and not locked \
                and bool(rec.approved_entry_count)
            rec.x_can_split = is_supervisor and not locked \
                and rec.state in ('draft', 'returned') and bool(rec.batch_ids)
            rec.x_can_acc_check = rec._is_accounting_pending_for(user)
            rec.x_is_pending_my_action = rec._is_pending_for(user)

    # ------------------------------------------------------------------
    # Accounting — who checks a department's commissions (Nov 2026)
    # ------------------------------------------------------------------
    def _accountant_users(self):
        """The department's Accounting Approvers, the same people who clear
        the accounting step of its time off (KSW_base_security). A handover
        with no department — a work site — falls to the company team.
        Identity reads, so sudo()."""
        accountants = self.env['hr.employee']
        for rec in self.sudo():
            accountants |= (rec.department_id.x_effective_accountant_ids
                            if rec.department_id else
                            self.env.company.sudo().x_default_accountant_ids)
        return accountants.sudo().user_id

    def _check_is_accountant(self):
        """Refuse unless the caller is one of this scope's accountants."""
        if self.env.su:
            return
        for rec in self:
            users = rec._accountant_users()
            if self.env.user in users:
                continue
            if not users:
                raise UserError(_(
                    "No Accounting Approver is set for %(scope)s. Ask HR to "
                    "set the department's Accounting Approvers.",
                    scope=rec.display_name))
            raise UserError(_(
                "Only the Accounting Approvers of %(scope)s can check its "
                "commissions: %(names)s.", scope=rec.display_name,
                names=', '.join(sorted(users.mapped('name')))))

    def _is_accounting_pending_for(self, user):
        self.ensure_one()
        return bool(self.sudo().acc_entry_count) \
            and user in self._accountant_users()

    def _is_pending_for(self, user):
        """A step here waits on ``user``: his check as one of the
        department's accountants, or his decision as its General Manager."""
        self.ensure_one()
        rec = self.sudo()
        gm_step = rec.run_id.state not in LOCKING_STATES \
            and bool(rec.pending_entry_count) and rec.gm_id == user
        return gm_step or rec._is_accounting_pending_for(user)

    def _search_is_pending_my_action(self, operator, value):
        # Odoo 19 hands '=' / '!=' over as 'in' / 'not in' (gotcha #24).
        if operator in ('in', 'not in'):
            positive = (operator == 'in') == any(value)
        elif operator in ('=', '!='):
            positive = (operator == '=') == bool(value)
        else:
            return NotImplemented
        user = self.env.user
        # Whatever has a row waiting on somebody — including a department
        # still being prepared with a sub-batch in it.
        candidates = self.env['ksw.pay.entry'].sudo().search([
            ('state', '=', 'submitted')]).batch_id.submission_id
        ids = candidates.filtered(lambda r: r._is_pending_for(user)).ids
        return [('id', 'in' if positive else 'not in', ids)]

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
        month and the submission if this is the first batch to need them.

        The month is the run of the department's General Manager: each GM
        has his own run per month (Oct 2026), so no GM ever opens a run
        holding another GM's departments.
        """
        period = fields.Date.to_date(period).replace(day=1)
        gm = gm_for_department(self.env, department)
        run = self.env['ksw.pay.run']._for_gm(period, gm)
        domain = [
            ('period', '=', period),
            ('department_id', '=', department.id if department else False),
            ('site_id', '=', site.id if site else False),
        ]
        submission = self.sudo().search(domain, limit=1)
        if submission and submission.run_id != run:
            submission._rehome_to_gm_run()
        if not submission:
            submission = self.sudo().create({
                'run_id': run.id,
                'department_id': department.id if department else False,
                'site_id': site.id if site else False,
            })
        return submission

    def _rehome_to_gm_run(self):
        """Move each submission onto the run of its department's current GM.

        HR can change a department's GM (or the company default) mid-month.
        A submission still in progress follows the department to its new
        GM's run; one already approved stays where it was signed, and a
        finalised run is never touched — that money is settled.
        Returns the runs that changed, so their registers can be rebuilt.
        """
        Run = self.env['ksw.pay.run']
        touched = Run.browse()
        for rec in self.sudo():
            if rec.state == 'approved' or rec.run_id.state in LOCKING_STATES:
                continue
            gm = gm_for_department(self.env, rec.department_id)
            if rec.run_id.gm_id == gm:
                continue
            target = Run._for_gm(rec.period, gm)
            if target.state in LOCKING_STATES:
                continue
            touched |= rec.run_id | target
            rec.write({'run_id': target.id})
        touched._sync_state()
        touched._refresh_register()
        return touched

    @api.model
    def _rehome_all_open(self):
        """Re-home every submission whose run no longer matches its GM."""
        subs = self.sudo().search([
            ('state', '!=', 'approved'),
            ('run_id.state', 'not in', LOCKING_STATES),
        ])
        return subs._rehome_to_gm_run()

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
        handed = self.sudo().batch_ids.entry_ids.filtered(
            lambda e: e.state != 'draft')
        if handed:
            raise UserError(_(
                "%(name)s holds entries the General Manager has or has "
                "approved, and cannot be deleted.",
                name=handed[0].batch_id.submission_id.display_name))
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
        """Hand this department's month over: to its accountants for their
        check first, then (action_acc_check) to the General Manager.

        Submits every batch still in draft on the way through, so the
        supervisor has one button to press rather than one per batch — and
        from this moment his batches are frozen until the GM returns them.
        """
        self._check_mine()
        for rec in self:
            check_period_unlocked(
                self.env, rec.period, _("Submitting this department"),
                gm=rec.sudo().run_id.gm_id)
            if rec.state == 'approved':
                raise UserError(_(
                    "%(name)s has already been approved.",
                    name=rec.display_name))
            if rec.state in HANDED_STATES:
                raise UserError(_(
                    "%(name)s has already been submitted.",
                    name=rec.display_name))
            if not rec._accountant_users():
                raise UserError(_(
                    "No Accounting Approver is set for %(name)s, so nobody "
                    "could check it before the General Manager. Ask HR to "
                    "set the department's Accounting Approvers.",
                    name=rec.display_name))
            if not rec.batch_ids:
                raise UserError(_(
                    "There is nothing to submit — no entries have been "
                    "recorded for %(name)s.", name=rec.display_name))
            # An empty batch records nothing, so it is no reason to refuse
            # the handover — and it cannot stay open inside a department
            # that has been handed over. It goes, and the chatter says so.
            rec._drop_empty_batches()
            if not rec.batch_ids:
                raise UserError(_(
                    "There is nothing to submit — no entries have been "
                    "recorded for %(name)s.", name=rec.display_name))

            drafts = rec.batch_ids.filtered(lambda b: b.state == 'draft')
            # A department coming back after a return (by the GM, or the
            # supervisor pulling a batch back) must reach the GM as such:
            # he approved nothing yet, but he may have looked, and the work
            # has changed since. Captured before the batches' own submit
            # clears their return reasons.
            resubmission = None
            if rec.state == 'returned':
                resubmission = {
                    'returned_by': rec.returned_by.name or '',
                    'reason': rec.return_reason or '',
                    'batches': [(b.name, b.return_reason or '') for b in drafts],
                }
            if drafts:
                drafts.action_submit()

            rec.write({
                'state': 'checking',
                'submitted_by': self.env.uid,
                'submitted_date': fields.Datetime.now(),
                'return_reason': False,
            })
            # A department whose every row was already approved in
            # sub-batches has nothing left waiting — it reads approved.
            rec._sync_state_from_entries()
            rec.sudo().sub_batch_ids._sync_state_from_entries()
            # Accounting acts on it. A resubmission also tells the GM what
            # changed since he returned it — he will be asked to approve it.
            partners = rec._accountant_users().partner_id
            if resubmission:
                partners |= rec._gm_partners()
            rec._notify_handover(partners, resubmission=resubmission)
        self.mapped('run_id')._sync_state()
        self.mapped('run_id')._refresh_register()
        return True

    def action_approve(self):
        """The GM signs off his own department, on its own — the rows its
        accountants have checked.

        Approval used to be one atomic act on the month: every submitted
        department at once, then build the register and lock. With a GM per
        department that is nobody's decision to make — so each department is
        approved by its own GM here, and closing the month becomes a separate
        act (see ksw.pay.run.action_approve / action_close_month).
        """
        self._check_is_my_department()
        approved = self.env['ksw.pay.entry']
        for rec in self:
            check_period_unlocked(
                self.env, rec.period, _("Approving this department"),
                gm=rec.sudo().run_id.gm_id)
            # Everything in front of him: the department's handover, and
            # any sub-batch sent early. Rows he returned are not in it.
            pending = rec._pending_entries()
            # An approved department can still have rows waiting: the GM
            # may add to a month after approving it (the lock exempts him).
            if rec.state == 'approved' and not pending:
                raise UserError(_(
                    "%(name)s has already been approved.",
                    name=rec.display_name))
            if not pending:
                if rec._acc_entries():
                    raise UserError(_(
                        "%(name)s is still with Accounting for checking. It "
                        "reaches you once they have checked it.",
                        name=rec.display_name))
                raise UserError(_(
                    "%(name)s has not been submitted yet, so there is "
                    "nothing to approve.", name=rec.display_name))
            pending._wf_approve()
            pending._sync_containers()
            approved |= pending
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
                     'emp': len(pending.employee_id),
                     'total': sum(pending.mapped('amount'))},
                subtype_xmlid='mail.mt_note',
            )
        runs = self.mapped('run_id')
        runs._sync_state()
        runs._refresh_register()
        # Rows added to a month already approved (only the GM may: the lock
        # exempts him) would be approved and never paid — an approved
        # register is frozen. Bring their lines up to the entries, the same
        # way a vacation settlement does.
        late = self.sudo().run_id.filtered(lambda r: r.state == 'approved')
        if late:
            late._resync_vacation_line(
                approved.employee_id,
                title=_('Register line updated: rows approved by the General '
                        'Manager after the month was approved'))
        # Approving the last department is the same decision whichever
        # screen it is made on, so the company GM is told here too. The
        # month itself is only ever finalised by him (action_close_month).
        runs._notify_ready_to_close()
        return True

    def action_return(self):
        """GM sends the department back, with the reason on every batch."""
        self._check_is_my_department()
        for rec in self:
            pending = rec._pending_entries()
            if not pending:
                raise UserError(_(
                    "Only a submitted department can be returned."))
            reason = (rec.return_reason or '').strip()
            if not reason:
                raise UserError(_(
                    "Say why it is being returned — the supervisor needs to "
                    "know what to fix. Type it in 'Return Reason' first."))
            rec.batch_ids.filtered(
                lambda b: b.state == 'submitted').action_return(reason)
            # Sub-batch rows sit in open batches, which action_return does
            # not reach — they go back the same way.
            rest = rec._pending_entries()
            rest._wf_return(reason)
            rest.x_sub_batch_id.sudo().write({'return_reason': reason})
            (pending | rest)._sync_containers()
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
                self.env, rec.period, _("Reopening this department"),
                gm=rec.sudo().run_id.gm_id)
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
    # Partial approval
    # ------------------------------------------------------------------
    def _pending_entries(self):
        """The rows in front of the General Manager for this department:
        handed over and checked by Accounting."""
        return self.sudo().batch_ids.entry_ids._awaiting_gm()

    def _acc_entries(self):
        """The rows in front of the department's accountants."""
        return self.sudo().batch_ids.entry_ids._awaiting_accounting()

    def _review_entries(self, mode):
        """What the review screen lists: rows to check (Accounting), rows
        to approve or return (the GM), or approved rows to reopen."""
        if mode == 'check':
            return self._acc_entries()
        if mode == 'review':
            return self._pending_entries()
        return self.sudo().batch_ids.entry_ids.filtered(
            lambda e: e.state == 'approved')

    def _open_batches(self):
        """The components still his to send: open batches with at least
        one draft row. A batch whose rows all went in a sub-batch has
        nothing to send — offering it only closed it for nothing."""
        return self.sudo().batch_ids.filtered(
            lambda b: b._has_something_to_send())

    def _open_sub_batches(self):
        """Sub-batches the supervisor has prepared and not yet sent."""
        return self.sudo().sub_batch_ids.filtered(
            lambda s: s.state in ('draft', 'returned')
            and s.entry_ids.filtered(lambda e: e.state == 'draft'))

    def _sync_state_from_entries(self):
        """Once handed over, the department reads what its rows say.

        Before the handover it is simply being prepared, whatever its
        sub-batches are doing — which is what stops approving one early
        employee from finalising the month.
        """
        for rec in self.sudo():
            if rec.state == 'draft':
                continue
            states = set(rec.batch_ids.entry_ids.mapped('state'))
            if not states:
                continue
            if 'draft' in states:
                target = 'returned'
            elif rec.batch_ids.entry_ids.filtered(
                    lambda e: e.state == 'submitted'
                    and not e.x_acc_checked_by):
                target = 'checking'
            elif 'submitted' in states:
                target = 'submitted'
            else:
                target = 'approved'
            if target != rec.state:
                rec.write({'state': target})
        return True

    def action_submit_prompt(self):
        """Submit to GM — some employees early, or the whole month?"""
        self._check_mine()
        return self.env['ksw.pay.submit.wizard']._open_for(self)

    def _drop_empty_batches(self):
        """Delete the draft batches with no entries, saying so on the
        department. Nothing is lost: an empty batch records nothing, and
        opening the component again next month recreates it."""
        for rec in self:
            empty = rec.sudo().batch_ids.filtered(
                lambda b: b.state == 'draft' and not b.entry_ids)
            if not empty:
                continue
            names = ', '.join('%s (%s)' % (b.name, b.component_id.name)
                              for b in empty)
            empty.unlink()
            rec.sudo().message_post(
                body=_("Empty batches removed on submission: %(names)s",
                       names=names),
                subtype_xmlid='mail.mt_note')
        return True

    # ------------------------------------------------------------------
    # Accounting's check (Nov 2026)
    # ------------------------------------------------------------------
    def action_acc_check(self):
        """Accounting: everything this department has with us is checked —
        on to the General Manager."""
        self._check_is_accountant()
        # Not refused in a finalised month: the GM may still add to it (the
        # lock exempts him), and a check moves no money — it only puts the
        # rows in front of him.
        for rec in self:
            rows = rec._acc_entries()
            if not rows:
                raise UserError(_(
                    "%(name)s has nothing waiting for Accounting's check.",
                    name=rec.display_name))
            rows._wf_acc_check()
            rows._sync_containers()
            rec._notify_checked(rows)
        return True

    def action_open_acc_review(self):
        """Accounting: the review screen, employee by employee — send some
        on to the GM, return others to the supervisor with a reason."""
        self.ensure_one()
        self._check_is_accountant()
        return self.env['ksw.pay.gm.review.wizard']._open_for(
            self, mode='check')

    def action_open_review(self):
        """GM: the review screen — component by component, approve or return
        the employees he ticks; the rest stay waiting on him."""
        self.ensure_one()
        self._check_is_my_department()
        return self.env['ksw.pay.gm.review.wizard']._open_for(
            self, mode='review')

    def action_open_reopen(self):
        """GM: reopen approved rows for correction, while they are unpaid."""
        self.ensure_one()
        self._check_is_my_department()
        return self.env['ksw.pay.gm.review.wizard']._open_for(
            self, mode='reopen')

    def action_open_sub_batches(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Sub-Batches — %(name)s', name=self.display_name),
            'res_model': 'ksw.pay.sub.batch',
            'view_mode': 'list,form',
            'domain': [('submission_id', '=', self.id)],
        }

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

    def _notify_checked(self, rows):
        """Accounting has checked: the GM is told, in his inbox and by
        email, that the department is now his to approve."""
        self.ensure_one()
        partners = self._gm_partners()
        self.sudo().message_post(
            body=Markup(
                '<strong>%(title)s</strong><br/>'
                '<b>%(l_dept)s</b> %(scope)s<br/>'
                '<b>%(l_by)s</b> %(user)s<br/>'
                '<b>%(l_emp)s</b> %(emp)s · <b>%(l_total)s</b> %(total).2f'
                '<br/><i>%(note)s</i>'
            ) % {'title': _('🧾 Checked by Accounting'),
                 'l_dept': _('Department:'), 'scope': self.display_name,
                 'l_by': _('By:'), 'user': self.env.user.name,
                 'l_emp': _('Employees:'), 'emp': len(rows.employee_id),
                 'l_total': _('Total:'), 'total': sum(rows.mapped('amount')),
                 'note': _('Now waiting for the General Manager\'s '
                           'approval.')},
            partner_ids=partners.ids,
            subtype_xmlid='mail.mt_comment',
        )
        self._email_gm(partners)

    def _email_gm(self, partners):
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

    def _notify_handover(self, partners, resubmission=None):
        """Tell whoever acts next that this department is in — the
        accountants at the handover (Nov 2026), in their inbox.

        ``resubmission`` (from action_submit) turns it into a "resubmitted
        after changes" message naming what was reopened, by whom and why.
        """
        self.ensure_one()
        if resubmission:
            body = Markup(
                '<strong>🔁 Resubmitted after changes</strong><br/>'
                '<b>Department:</b> %(scope)s<br/>'
                '<b>Resubmitted by:</b> %(user)s<br/>'
                '<b>Returned by:</b> %(returned_by)s<br/>'
                '<b>Reason:</b> %(reason)s<br/>'
                '<b>Total now:</b> %(total).2f<br/>'
                '<b>Batches reopened and edited:</b><ul>'
            ) % {'scope': self.display_name, 'user': self.env.user.name,
                 'returned_by': resubmission['returned_by'] or '—',
                 'reason': resubmission['reason'] or '—',
                 'total': self.total_amount or 0.0}
            for name, reason in resubmission['batches']:
                body += Markup('<li>%(name)s%(reason)s</li>') % {
                    'name': name,
                    'reason': Markup(' — <i>%s</i>') % reason if reason else ''}
            body += Markup('</ul><i>Please review the changes before approving.</i>')
            self.sudo().message_post(
                body=body, partner_ids=partners.ids,
                subtype_xmlid='mail.mt_comment')
            return
        self.sudo().message_post(
            body=Markup(
                '<strong>📤 Submitted for approval</strong><br/>'
                '<b>Department:</b> %(scope)s<br/>'
                '<b>By:</b> %(user)s<br/>'
                '<b>Batches:</b> %(batches)s · '
                '<b>Employees:</b> %(emp)s<br/>'
                '<b>Total:</b> %(total).2f<br/>'
                '<i>With Accounting for checking; it goes on to the General '
                'Manager once checked.</i>'
            ) % {'scope': self.display_name, 'user': self.env.user.name,
                 'batches': self.batch_count, 'emp': self.employee_count,
                 'total': self.total_amount or 0.0},
            partner_ids=partners.ids,
            subtype_xmlid='mail.mt_comment',
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
        """Every employee in this department, one review page each.

        This is the detail behind the department's total: who is being paid,
        for what, and how much each of them adds up to. The page splits an
        employee's entries by component, each with its own subtotal, and its
        pager walks the reviewer to the next employee.
        """
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Employees — %(name)s', name=self.display_name),
            'res_model': 'ksw.pay.employee.review',
            'view_mode': 'list,form',
            'domain': [('submission_id', '=', self.id)],
        }
