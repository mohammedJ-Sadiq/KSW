"""KSW Pay Run — the month, its approval, and what actually gets paid.

One run per month. Supervisors submit their batches into it; the General
Manager approves the whole month in one action, which is what turns entries
into money:

* every submitted batch is marked approved;
* a **payment register** is built — one line per employee, generated, never
  hand-maintained. It is the printable per-employee document and the thing the
  bank export reads;
* parked loan installments are settled out of the payment;
* the period is locked, through the single predicate every mutation route
  already calls.

The register is what replaced the old per-employee commission sheet. It carries
the same information and feeds the same bank file, but it is derived rather
than typed, so there is no second document to keep in step and no second
approval to chase.
"""
import json
import math

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare, float_round
from odoo.tools.misc import format_datetime

from .ksw_commission_lock import LOCKING_STATES
from .ksw_vacation_hold import (
    hold_blocks, hold_reason, vacation_holds,
)

RUN_STATES = [
    ('open', 'Open'),
    ('submitted', 'Submitted to GM'),
    ('approved', 'Approved'),
    ('paid', 'Paid'),
]


class KswPayRun(models.Model):
    _name = 'ksw.pay.run'
    _description = 'KSW Monthly Pay Run'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'period desc, id desc'

    name = fields.Char(readonly=True, default='New', copy=False)
    period = fields.Date(
        required=True, tracking=True,
        default=lambda s: fields.Date.context_today(s).replace(day=1),
    )
    state = fields.Selection(
        RUN_STATES, default='open', required=True, copy=False, tracking=True,
    )
    currency_id = fields.Many2one(
        'res.currency', required=True,
        default=lambda s: s.env.company.currency_id,
    )

    line_ids = fields.One2many(
        'ksw.pay.run.line', 'run_id', string='Payment Register',
    )
    submission_ids = fields.One2many(
        'ksw.pay.submission', 'run_id', string='Department Submissions',
    )
    # Scoped per user on purpose: a supervisor opening the shared run must see
    # his own batches, not the company's. Goes through search() because
    # reading a plain one2many does not apply the comodel's record rules —
    # only search() does. Same reason for the two fields below it.
    batch_ids = fields.Many2many(
        'ksw.pay.batch', compute='_compute_batch_ids', string='Batches',
    )
    x_visible_submission_ids = fields.Many2many(
        'ksw.pay.submission', compute='_compute_visible_submissions',
        string='Departments',
    )
    x_visible_line_ids = fields.Many2many(
        'ksw.pay.run.line', compute='_compute_visible_lines',
        string='Who Gets Paid',
    )

    # The supervisor's own handover, surfaced on the shared month so his one
    # button lives where he was already looking for it.
    x_my_submission_ids = fields.Many2many(
        'ksw.pay.submission', compute='_compute_visible_submissions',
        string='My Departments',
    )
    x_can_submit_mine = fields.Boolean(compute='_compute_visible_submissions')
    submitted_department_count = fields.Integer(
        compute='_compute_submission_stats')
    pending_department_count = fields.Integer(
        compute='_compute_submission_stats')
    pending_department_names = fields.Char(
        compute='_compute_submission_stats')

    x_salary_bank_account_id = fields.Many2one(
        'res.partner.bank', string='Default Paying Bank Account',
        help='Fallback for employees with no personal salary bank account.',
    )
    note = fields.Text()

    submitted_by = fields.Many2one('res.users', readonly=True, copy=False)
    submitted_date = fields.Datetime(readonly=True, copy=False)
    approved_by = fields.Many2one('res.users', readonly=True, copy=False)
    approved_date = fields.Datetime(readonly=True, copy=False)

    batch_count = fields.Integer(compute='_compute_batch_ids')
    draft_batch_count = fields.Integer(compute='_compute_batch_ids')
    employee_count = fields.Integer(compute='_compute_totals', store=True)
    total_earnings = fields.Monetary(compute='_compute_totals', store=True)
    total_loan_offset = fields.Monetary(compute='_compute_totals', store=True)
    total_payable = fields.Monetary(compute='_compute_totals', store=True)

    # Button gates — per user, so the same shared run offers each role only
    # what it may actually do.
    x_can_submit = fields.Boolean(compute='_compute_permissions')
    x_can_approve = fields.Boolean(compute='_compute_permissions')
    x_can_close_month = fields.Boolean(compute='_compute_permissions')
    x_can_reopen = fields.Boolean(compute='_compute_permissions')
    x_can_export = fields.Boolean(compute='_compute_permissions')

    # What this month will NOT pay, shown before the GM approves rather
    # than explained in the chatter afterwards. No model-level groups= —
    # the form reads it in an invisible= expression (Odoo 19 Pitfalls #31).
    x_vacation_held_warning = fields.Text(
        compute='_compute_vacation_held_warning', compute_sudo=True,
        string='Held for Vacation',
    )

    # The submitted departments this user is the GM of — what his Approve
    # button will actually act on, and the list the form shows him.
    x_gm_submission_ids = fields.Many2many(
        'ksw.pay.submission', compute='_compute_visible_submissions',
        string='Awaiting My Approval',
    )

    _unique_period = models.Constraint(
        'UNIQUE(period)', 'There is already a pay run for that month.')

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends_context('uid')
    @api.depends('period')
    def _compute_batch_ids(self):
        Batch = self.env['ksw.pay.batch']
        for rec in self:
            batches = Batch.search([('period', '=', rec.period)])
            rec.batch_ids = batches
            rec.batch_count = len(batches)
            rec.draft_batch_count = len(
                batches.filtered(lambda b: b.state == 'draft'))

    @api.depends_context('uid')
    @api.depends('submission_ids.state', 'submission_ids.gm_id')
    def _compute_visible_submissions(self):
        Submission = self.env['ksw.pay.submission']
        allowed = self.env['ksw.pay.batch']._allowed_departments()
        is_officer = self.env.user.has_group(
            'KSW_commissions.group_commission_officer')
        user = self.env.user
        for rec in self:
            visible = Submission.search([('run_id', '=', rec.id)])
            rec.x_visible_submission_ids = visible
            # Anything in front of him — a handed-over department or a
            # sub-batch sent early — not only departments on 'submitted'.
            rec.x_gm_submission_ids = visible.filtered(
                lambda s: s.pending_entry_count and s.gm_id == user)
            # "Mine" is narrower than "visible": an officer sees every
            # department but hands over none of them.
            mine = visible if not is_officer else visible.filtered(
                lambda s: s.department_id and s.department_id in allowed)
            rec.x_my_submission_ids = mine
            rec.x_can_submit_mine = bool(
                rec.state not in LOCKING_STATES
                and mine.filtered(lambda s: s.x_can_submit))


    @api.depends('submission_ids.state', 'submission_ids.batch_count')
    def _compute_submission_stats(self):
        for rec in self:
            active = rec.submission_ids.filtered('batch_count')
            done = active.filtered(lambda s: s.state in ('submitted',
                                                         'approved'))
            pending = active - done
            rec.submitted_department_count = len(done)
            rec.pending_department_count = len(pending)
            rec.pending_department_names = ', '.join(
                pending.mapped('display_name')) or ''

    @api.depends_context('uid')
    @api.depends('line_ids.net_payable', 'line_ids.earnings')
    def _compute_visible_lines(self):
        Line = self.env['ksw.pay.run.line']
        for rec in self:
            rec.x_visible_line_ids = Line.search([('run_id', '=', rec.id)])

    @api.depends('line_ids.net_payable', 'line_ids.earnings',
                 'line_ids.loan_offset')
    def _compute_totals(self):
        for rec in self:
            rec.employee_count = len(rec.line_ids)
            rec.total_earnings = sum(rec.line_ids.mapped('earnings'))
            rec.total_loan_offset = sum(rec.line_ids.mapped('loan_offset'))
            rec.total_payable = sum(rec.line_ids.mapped('net_payable'))

    @api.depends_context('uid')
    @api.depends('state', 'submitted_department_count', 'x_gm_submission_ids',
                 'submission_ids.state')
    def _compute_permissions(self):
        user = self.env.user
        is_supervisor = user.has_group(
            'KSW_commissions.group_commission_supervisor')
        is_accountant = user.has_group(
            'KSW_commissions.group_commission_accountant')
        is_closer = self._is_month_closer()
        for rec in self:
            rec.x_can_submit = is_supervisor and rec.state == 'open'
            # Approve now means "approve MY departments". A GM with nothing
            # of his own waiting has nothing to approve, however senior he is.
            rec.x_can_approve = bool(
                rec.state not in LOCKING_STATES and rec.x_gm_submission_ids)
            # Finalising is the separate, deliberate act of declaring the
            # month finished and handing it to the accountant. It is not any
            # single GM's call, so it belongs to whoever owns the month.
            #
            # It used to also require a department still stuck on
            # 'submitted', on the reasoning that a month with nothing left
            # waiting finalises itself. It does — but only down the run's own
            # Approve button. A GM who signed his departments off on their
            # submission forms instead reached exactly the same place with
            # nothing to show for it: no department waiting, so no Approve
            # button; no department unapproved, so no close button either;
            # and the accountant's export gated on a state the month could no
            # longer reach. One approved department is enough to offer it.
            rec.x_can_close_month = bool(
                is_closer
                and rec.state not in LOCKING_STATES
                and rec._has_approved_work())
            # A paid month is history: the money left the bank, and
            # reopening it would restate what was paid (sub-batch rows
            # included, which the GM promised the supervisor were final).
            rec.x_can_reopen = is_closer and rec.state == 'approved'
            rec.x_can_export = is_accountant and rec.state in LOCKING_STATES

    # Deliberately NOT depending on batch_ids.entry_ids: batch_ids is a
    # non-searchable compute, so the ORM cannot work back from an entry to
    # the runs to recompute (it warns about exactly that at load). The
    # field is non-stored and the form recomputes on open, which is when
    # the GM reads it.
    @api.depends('period', 'state', 'submission_ids.state',
                 'line_ids.employee_id')
    def _compute_vacation_held_warning(self):
        for rec in self:
            rec.x_vacation_held_warning = False
            # Same reasoning as the batch banner: an approved or paid month
            # is history, and _build_register never touches it again.
            if not rec.period or rec.state in LOCKING_STATES:
                continue
            held = rec._held_entries()
            if not held:
                continue
            rec.x_vacation_held_warning = _(
                "%(count)s entr%(plural)s worth %(amount).2f will NOT be "
                "paid: %(who)s went on vacation, and what they had earned "
                "was settled on the leave request itself. Release the month "
                "under Vacation Releases if it was never listed there.",
                count=len(held),
                plural=_('y') if len(held) == 1 else _('ies'),
                amount=sum(held.mapped('amount')),
                who=', '.join(held.employee_id.sudo().mapped('display_name')),
            )

    @api.depends('period')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = rec.period.strftime('%B %Y') \
                if rec.period else (rec.name or '')

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        Seq = self.env['ir.sequence']
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == 'New':
                vals['name'] = Seq.next_by_code('ksw.pay.run') or 'New'
            if vals.get('period'):
                vals['period'] = fields.Date.to_date(
                    vals['period']).replace(day=1)
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('period'):
            vals['period'] = fields.Date.to_date(vals['period']).replace(day=1)
        if not self.env.su:
            protected = set(vals) - {
                'state', 'note', 'submitted_by', 'submitted_date',
                'approved_by', 'approved_date', 'x_salary_bank_account_id',
                'message_follower_ids', 'message_ids', 'activity_ids',
                'message_main_attachment_id',
            }
            for rec in self:
                if rec.state in LOCKING_STATES and protected:
                    raise UserError(_(
                        "%(name)s has been approved. Reopen it before "
                        "changing it.", name=rec.display_name))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            locked = self.filtered(lambda r: r.state in LOCKING_STATES)
            if locked:
                raise UserError(_(
                    "An approved pay run cannot be deleted: %(names)s",
                    names=', '.join(locked.mapped('display_name'))))
        return super().unlink()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _check_group(self, xmlid, message):
        """Server-side authorisation. View-level invisible= is cosmetic —
        any user with ORM write access can call these over RPC."""
        if self.env.su or self.env.user.has_group(xmlid):
            return
        raise UserError(message)

    def _all_batches(self):
        """Every batch for the month, company-wide.

        sudo() on purpose, unlike the display field: a supervisor's partial
        view must not be able to wave through another department's drafts.
        """
        self.ensure_one()
        return self.env['ksw.pay.batch'].sudo().search([
            ('period', '=', self.period)])

    def _all_entries(self):
        self.ensure_one()
        return self.env['ksw.pay.entry'].sudo().search([
            ('period', '=', self.period)])

    def _has_approved_work(self):
        """Is there anything the General Manager has approved this month —
        a whole department, or employees approved early in a sub-batch?"""
        self.ensure_one()
        return bool(self.submission_ids.filtered(
            lambda s: s.state == 'approved')) or bool(
            self.env['ksw.pay.entry'].sudo().search_count([
                ('period', '=', self.period), ('state', '=', 'approved')],
                limit=1))

    def _waiting_submissions(self):
        """Departments with rows in front of their GM, whatever their own
        state says — a sub-batch waits on him as much as a handover does."""
        return self.submission_ids.filtered('pending_entry_count')

    def _payable_entries(self, settled_only=False):
        """The entries this month pays — decided per row, not per batch.

        Settled: every row its General Manager approved, whether with its
        whole department or early, in a sub-batch. Preview: those, plus the
        rows in front of him now (_handed_over) — a batch merely submitted
        on its own is a supervisor who has finished typing, not a department
        declaring itself complete, so it is still left out.
        """
        self.ensure_one()
        entries = self._all_entries()
        if settled_only:
            return entries.filtered(lambda e: e.state == 'approved')
        return entries.filtered(lambda e: e.state == 'approved') \
            | entries._handed_over()

    def _payable_batches(self, settled_only=False):
        """The batches whose entries are actually going to be paid.

        A batch counts only once its **department** has been handed over —
        a submitted batch on its own is just a supervisor who has finished
        typing, not a department declaring itself complete.

        `settled_only` is the difference between the preview and the real
        thing. The preview shows every handed-over department, because
        knowing who is *about* to be paid is the point of showing it early.
        The register built at finalisation may only contain departments
        their own General Manager actually approved — otherwise closing a
        month would pay a department whose GM never looked at it, which is
        precisely what splitting the approval was meant to prevent.
        """
        self.ensure_one()
        if settled_only:
            return self._all_batches().filtered(
                lambda b: b.submission_id.state == 'approved')
        return self._all_batches().filtered(
            lambda b: b.state == 'approved' or (
                b.state == 'submitted'
                and b.submission_id.state in ('submitted', 'approved')))

    def _sync_state(self):
        """Keep the month's status in step with its departments.

        The run's state is a summary, not something anyone sets by hand: it
        reads 'Submitted to GM' once every department that has entries has
        handed over, and drops back to 'Open' when one is returned.
        """
        for rec in self:
            if rec.state in LOCKING_STATES:
                continue
            active = rec.submission_ids.filtered('batch_count')
            done = active.filtered(
                lambda s: s.state in ('submitted', 'approved'))
            target = 'submitted' if (active and done == active) else 'open'
            if rec.state != target:
                rec.sudo().write({'state': target})
        return True

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def action_submit_my_departments(self):
        """The supervisor's button: hand over **only** his own records.

        This is the whole point of the department submission. The month is
        shared, so submitting it wholesale was never his to do — he would
        have been declaring six other departments complete. Here he presses
        one button and only his batches move.
        """
        self.ensure_one()
        mine = self.x_my_submission_ids.filtered(lambda s: s.x_can_submit)
        if not mine:
            raise UserError(_(
                "You have nothing to submit for %(period)s. Record your "
                "entries first — or they have already been submitted.",
                period=self.display_name))
        # He chooses what goes: every component, or only the ones he ticks.
        mine._check_mine()
        return self.env['ksw.pay.submit.wizard']._open_for(mine)

    def action_submit(self):
        """open → submitted, by hand.

        Kept for the Officer who is closing the month on everyone's behalf;
        supervisors go through their own department. Normally this state is
        reached on its own as the last department hands over.
        """
        self._check_group(
            'KSW_commissions.group_commission_officer',
            _("Only a Commission Officer can close the month by hand. "
              "Supervisors submit their own department."))
        for rec in self:
            if rec.state != 'open':
                raise UserError(_(
                    "Only an open pay run can be submitted."))
            if not rec.submitted_department_count:
                raise UserError(_(
                    "No department has submitted anything for %(period)s "
                    "yet.", period=rec.display_name))
            rec.write({
                'state': 'submitted',
                'submitted_by': self.env.uid,
                'submitted_date': fields.Datetime.now(),
            })
            rec._notify_gm()
        return True

    def _notify_gm(self):
        self.ensure_one()
        group = self.env.ref('KSW_commissions.group_commission_gm',
                             raise_if_not_found=False)
        partners = group.sudo().all_user_ids.partner_id if group else None
        self.sudo().message_post(
            body=Markup(
                '<strong>📤 Submitted for approval</strong><br/>'
                '<b>Month:</b> %(period)s<br/>'
                '<b>Departments in:</b> %(subs)s<br/>'
                '<b>By:</b> %(user)s'
            ) % {'period': self.display_name,
                 'subs': self.submitted_department_count,
                 'user': self.env.user.name},
            partner_ids=partners.ids if partners else [],
            subtype_xmlid='mail.mt_comment',
        )

    @api.model
    def _is_month_closer(self):
        """Who may declare the month finished and lock it.

        Approving is now per department, so no single GM owns the month any
        more — but somebody still has to decide that a department which never
        handed over will not be paid, and to lock the period. That is a
        company-level call, and it stays with the company's General Manager
        (`res.company.x_default_gm_id`) — the person who was doing all of
        this before the split.

        Deliberately NOT the Commission Officer: reopening a month unwinds
        the loan offsets of every department at once, and that has never
        been the Officer's to do (tests/test_period_lock.py
        ::test_10_only_the_gm_reopens).
        """
        if self.env.su:
            return True
        # The system administrator is not a second General Manager — he is
        # the way out when the configured one is absent, has left, or was
        # never set on the company at all. Without him a month whose GM is
        # gone can never be handed to the accountant by anybody.
        #
        # Two keys, deliberately: this grants the *authority*, his
        # Commission Role grants the *reach*. base.group_system holds no
        # ACL on ksw.pay.run, so a Settings administrator with no commission
        # role cannot read the month, let alone finalise it (gotcha #38).
        # Implying Administrator from it instead would put every Settings
        # admin into the Commission Role single-select, which is the whole
        # separation of duties.
        if self.env.user.has_group('base.group_system'):
            return True
        default_gm = self.env.company.sudo().x_default_gm_id
        return bool(default_gm and default_gm.sudo().user_id == self.env.user)

    def action_approve(self):
        """The GM approves **his own** departments' handovers.

        Approval used to be one atomic act over every submitted department,
        which only made sense while one GM answered for all of them. Each GM
        now signs off what is his; the month finalises by itself as soon as
        nothing is left waiting, and otherwise waits for a deliberate close.
        """
        for rec in self:
            if rec.state in LOCKING_STATES:
                raise UserError(_(
                    "%(name)s has already been approved.",
                    name=rec.display_name))
            mine = rec.x_gm_submission_ids
            if not mine:
                waiting = rec.submission_ids.filtered(
                    lambda s: s.state == 'submitted')
                if not waiting:
                    raise UserError(_(
                        "No department has submitted its commissions for "
                        "%(period)s, so there is nothing to approve.",
                        period=rec.display_name))
                raise UserError(_(
                    "None of the departments waiting on %(period)s are "
                    "yours to approve. Still waiting on their own General "
                    "Managers: %(names)s.",
                    period=rec.display_name,
                    names=', '.join(waiting.mapped('display_name'))))
            # Approving the submissions is what finalises the month when
            # they were the last ones waiting — see _finalise_if_complete,
            # which both this button and the submission form go through.
            mine.action_approve()
            if rec.state in LOCKING_STATES:
                continue

            still_waiting = rec._unapproved_departments()
            rec.sudo().message_post(
                body=Markup(
                    '<strong>✅ %(mine)s department(s) approved by '
                    '%(user)s</strong><br/>'
                    '<i>%(period)s stays open — not approved yet: '
                    '%(names)s.</i>'
                ) % {'mine': len(mine), 'user': self.env.user.name,
                     'period': rec.display_name,
                     'names': ', '.join(
                         still_waiting.mapped('display_name'))},
                subtype_xmlid='mail.mt_note',
            )
            rec._refresh_register()
        return True

    def _finalise_if_complete(self):
        """Lock the month the moment no department is left waiting.

        One predicate, called from every approval route. A GM who signs a
        department off on its own submission form has made exactly the same
        decision as one who pressed Approve on the month, so the month has
        to finalise either way — it used to finalise only down the run's
        button, which is how a fully approved month ended up with no button
        on it at all and no way to reach the accountant.

        Only when **every** department with entries is fully approved — the
        user's rule (Sep 2026). It used to fire as soon as one department
        was approved and none was waiting, leaving a department still typing
        out of the month; with partial approvals that locked a whole month
        mid-way (dev, September 2026: Maintenance and Train Department shut
        out). Anything short of "all in" is the company GM's call, through
        Finalise & Send to Accounting.
        """
        for rec in self:
            if rec.state in LOCKING_STATES:
                continue
            if rec._unapproved_departments() or rec._waiting_submissions():
                continue
            if not rec._active_submissions():
                continue
            rec._finalise_month()
        return True

    def _active_submissions(self):
        """Departments that recorded anything this month."""
        self.ensure_one()
        return self.submission_ids.filtered(
            lambda s: s.sudo().batch_ids.entry_ids)

    def _unapproved_departments(self):
        """Departments with entries that are not fully approved yet —
        still typing, handed over, or partly returned."""
        self.ensure_one()
        return self._active_submissions().filtered(
            lambda s: s.state != 'approved')

    def action_close_month(self):
        """Finalise the month by hand and hand it to the accountant.

        The deliberate counterpart to the automatic finalisation above, and
        the button that is always there when it did not happen: only what
        each department's own General Manager approved gets paid, and
        anything still waiting or never submitted keeps its work in draft
        rather than being swept into a month it never declared itself ready
        for.
        """
        if not self._is_month_closer():
            raise UserError(_(
                "Only the company's General Manager or a system "
                "administrator can finalise the month."))
        for rec in self:
            if rec.state in LOCKING_STATES:
                raise UserError(_(
                    "%(name)s has already been approved.",
                    name=rec.display_name))
            if not rec._has_approved_work():
                raise UserError(_(
                    "No department has been approved for %(period)s, so "
                    "there is nothing to pay.", period=rec.display_name))
            rec._finalise_month()
        return True

    def _finalise_month(self):
        """Build the register, settle the loans and lock the period."""
        self.ensure_one()
        approved = self.submission_ids.filtered(
            lambda s: s.state == 'approved')
        never_submitted = self.pending_department_names
        unapproved = self._waiting_submissions()
        self._build_register()
        auto_ids = self.line_ids.sudo()._auto_flag_priority_installments()
        self.write({
            'state': 'approved',
            'approved_by': self.env.uid,
            'approved_date': fields.Datetime.now(),
        })
        self.line_ids.sudo()._apply_loan_offset()
        self.line_ids.sudo()._unflag_uncovered_auto_installments(auto_ids)
        body = Markup(
            '<strong>✅ Approved</strong><br/>'
            '<b>Departments:</b> %(depts)s<br/>'
            '<b>Employees:</b> %(count)s<br/>'
            '<b>Earnings:</b> %(earn).2f<br/>'
            '<b>Loans settled:</b> %(loans).2f<br/>'
            '<b>Payable:</b> %(pay).2f<br/>'
            '<i>%(period)s is now locked.</i>'
        ) % {'depts': len(approved), 'count': self.employee_count,
             'earn': self.total_earnings or 0.0,
             'loans': self.total_loan_offset or 0.0,
             'pay': self.total_payable or 0.0,
             'period': self.display_name}
        if never_submitted:
            body += Markup(
                '<br/><b>⚠ Not included</b> (never submitted): %(names)s'
            ) % {'names': never_submitted}
        if unapproved:
            body += Markup(
                '<br/><b>⚠ Not included</b> (submitted but never approved '
                'by their General Manager): %(names)s'
            ) % {'names': ', '.join(unapproved.mapped('display_name'))}
        # Finalising the month *is* the handover to accounting, so it is
        # addressed to them rather than filed as an internal note: the bank
        # export is the next thing that has to happen and nobody was being
        # told it could.
        accountants = self._accountant_partners()
        if accountants:
            body += Markup(
                '<br/><i>Ready for the bank export.</i>')
        self.sudo().message_post(
            body=body,
            partner_ids=accountants.ids,
            subtype_xmlid='mail.mt_comment' if accountants
            else 'mail.mt_note',
        )
        return True

    def _accountant_partners(self):
        group = self.env.ref('KSW_commissions.group_commission_accountant',
                             raise_if_not_found=False)
        if not group:
            return self.env['res.partner']
        return group.sudo().all_user_ids.partner_id

    def action_return_to_supervisors(self):
        """submitted → open, so corrections can be made."""
        if not self._is_month_closer():
            raise UserError(_(
                "Only the company's General Manager can return the whole "
                "month. To send back one department, use Return on its own "
                "submission."))
        for rec in self:
            if rec.state != 'submitted':
                raise UserError(_(
                    "Only a submitted pay run can be returned."))
            rec.write({'state': 'open', 'submitted_by': False,
                       'submitted_date': False})
        return True

    def action_open_my_submission(self):
        """Take the supervisor straight to his own department's handover."""
        self.ensure_one()
        mine = self.x_my_submission_ids
        if not mine:
            raise UserError(_(
                "You have no department on %(period)s. Record an entry "
                "first — or ask HR to set you as your department's manager.",
                period=self.display_name))
        if len(mine) == 1:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'ksw.pay.submission',
                'res_id': mine.id,
                'view_mode': 'form',
            }
        return {
            'type': 'ir.actions.act_window',
            'name': _('My Departments'),
            'res_model': 'ksw.pay.submission',
            'view_mode': 'list,form',
            'domain': [('id', 'in', mine.ids)],
        }

    def action_open_register(self):
        """The register as a real list — groupable, and rule-scoped."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Payment Register — %(name)s', name=self.display_name),
            'res_model': 'ksw.pay.run.line',
            'view_mode': 'list,form',
            'domain': [('run_id', '=', self.id)],
            'context': {'search_default_grp_department': 1},
        }

    def action_reopen(self):
        """Unlock an approved month.

        Whoever owns the month, not any single department GM: reopening
        unwinds the loan offsets for every department at once, including
        ones that are none of his.
        """
        if not self._is_month_closer():
            raise UserError(_(
                "Only the company's General Manager can reopen an "
                "approved month."))
        for rec in self:
            if rec.state == 'paid':
                raise UserError(_(
                    "%(name)s has been paid. A paid month cannot be "
                    "reopened — not even by the General Manager.",
                    name=rec.display_name))
            if rec.state not in LOCKING_STATES:
                raise UserError(_(
                    "%(name)s is not approved, so there is nothing to "
                    "reopen.", name=rec.display_name))
            # Give the installments back before the lock lifts.
            rec.line_ids.sudo()._unwind_loan_offset()
            # Everything the approval moved forward moves back one step: the
            # departments are still handed over, they are simply no longer
            # approved. Leaving the batches on 'approved' would keep them
            # payable even after their department withdrew.
            #
            # Per row: a row approved early in a sub-batch was its own
            # decision and stays approved; so does anything already paid on
            # a vacation payslip.
            reopened = rec.submission_ids.filtered(
                lambda s: s.state == 'approved')
            rows = reopened.sudo().batch_ids.entry_ids.filtered(
                lambda e: e.state == 'approved'
                and not e.x_vacation_payslip_id)
            (rows - rows._sub_batch_controlled()).write(
                {'state': 'submitted'})
            reopened.mapped('batch_ids').filtered(
                lambda b: b.state == 'approved'
            ).sudo().with_context(ksw_entry_sync=True).write(
                {'state': 'submitted'})
            reopened.sudo().write({'state': 'submitted'})
            reopened.mapped('batch_ids')._sync_state_from_entries()
            reopened._sync_state_from_entries()
            rec.write({'state': 'open'})
            rec.sudo().message_post(
                body=Markup(
                    '<strong>🔓 Reopened</strong><br/><b>By:</b> %s<br/>'
                    '<i>The settled loan installments were returned to '
                    'pending and the register is a preview again.</i>'
                ) % self.env.user.name,
                subtype_xmlid='mail.mt_note',
            )
            # The register goes back to being a preview rather than
            # disappearing: the departments are still handed over, so the
            # GM keeps seeing who is due to be paid while he corrects.
            rec._sync_state()
            rec._refresh_register()
        return True

    def action_mark_paid(self):
        self._check_group(
            'KSW_commissions.group_commission_accountant',
            _("Only the Commission Accountant can mark the month paid."))
        for rec in self:
            if rec.state != 'approved':
                raise UserError(_(
                    "Only an approved pay run can be marked paid."))
            rec._commit_to_employees()
            rec.write({'state': 'paid'})
        return True

    # ------------------------------------------------------------------
    # The payment register
    # ------------------------------------------------------------------
    def _build_register(self, preview=False):
        """One line per employee with anything to be paid this month.

        Built from the moment work is handed over, not only at approval —
        knowing *who* is about to be paid, and what they consume in loan
        repayments, is exactly what the supervisor and the GM need in order
        to review the month rather than rubber-stamp it. Until approval the
        loan figure is an estimate of what would be settled; approval turns
        it into the settlement itself.
        """
        self.ensure_one()
        Line = self.env['ksw.pay.run.line'].sudo()

        entries = self._payable_entries(
            settled_only=not preview).filtered('employee_id')

        # The last of the three gates on the vacation hold, and the one
        # that matters most: the entry guard only ever sees rows typed
        # after it existed, and a batch submitted before that carries its
        # own. Whatever reaches the register, the register does not pay it.
        held = self._held_entries(entries)
        # Paid on a vacation payslip already (KSW_COM_ inputs): the entry
        # stays on its batch as the record, the register does not pay it.
        settled = entries.filtered('x_vacation_payslip_id')
        payable = entries - held - settled

        totals = {
            employee_id: sum(by_component.values())
            for employee_id, by_component
            in self._rounded_component_totals(payable).items()
        }
        if held and not preview:
            self._announce_held(held)
        if settled and not preview:
            self._announce_settled(settled)

        lines = self.sudo().line_ids
        existing = {line.employee_id.id: line for line in lines}

        # A preview owns only what a previous preview created. Anything else
        # on this register is a settled figure — carried over from the
        # commission sheets this app replaced, or left by a reopened month —
        # and rebuilding a projection is no reason to destroy it.
        stale = lines.filtered(lambda l: l.employee_id.id not in totals)
        if preview:
            stale = stale.filtered('x_preview_generated')
        if stale:
            stale.unlink()

        for employee_id, amount in totals.items():
            line = existing.get(employee_id)
            if line and line.exists():
                if preview and not line.x_preview_generated:
                    continue
                line.write({'earnings': amount})
            else:
                line = Line.create({
                    'run_id': self.id, 'employee_id': employee_id,
                    'earnings': amount,
                    'x_preview_generated': True,
                })
            if preview:
                line.loan_offset = math.floor(
                    min(amount, line._pending_loan_total()) + 1e-6)
            else:
                # From approval on, this is the settlement itself.
                line.x_preview_generated = False
        return self.line_ids

    @api.model
    def _months_paid_to(self, employee, periods):
        """The months in ``periods`` whose run has paid ``employee``: THE
        predicate against paying a commission twice.

        Only **Paid** counts — that is the accountant's statement that the
        transfer went out. An approved month, exported or not, is still
        open: a vacation settles his whole account on the day he leaves,
        takes those entries, and ``_resync_vacation_line`` takes them out of
        the register (the bank file must then be exported again). A run
        with no line for him paid him nothing.

        Read by every route: which entries a vacation lists and pays
        (``hr.leave._commission_entries_outstanding``), the payslip confirm
        guard, and the entry-level stamp guard (``ksw.pay.entry.write``).
        """
        return set(self.env['ksw.pay.run.line'].sudo().search([
            ('employee_id', '=', employee.id),
            ('run_id.period', 'in', list(periods)),
            ('run_id.state', '=', 'paid'),
        ]).mapped('run_id.period'))

    def _commit_to_employees(self):
        """The run is being marked Paid: make sure no vacation pays it.

        First the register is brought in step with every vacation payslip
        already confirmed — a settled entry must never be paid here too.
        Then whatever an unconfirmed vacation / EOS payslip still carries
        for this month is taken off it at once.
        """
        self.ensure_one()
        run = self.sudo()
        settled = run._all_entries().filtered('x_vacation_payslip_id')
        if settled and run.state == 'approved':
            run._resync_vacation_line(settled.employee_id)
        if run.line_ids:
            self.env['hr.payslip']._ksw_drop_committed_commissions(
                run, run.line_ids.employee_id)

    def _resync_vacation_line(self, employees):
        """Re-derive an approved month's register line for ``employees``
        after a vacation payslip took entries out of it (or gave them back).

        An approved register is otherwise frozen (``_refresh_register``
        skips it), so without this the bank file would pay the entries a
        second time, and the BAS journal — which reads the entries minus the
        settled ones — would stop on the mismatch. The line's figure is the
        journal's own figure, so the two cannot disagree.

        The loan offset is unwound and re-applied against the new earnings.
        Whatever it no longer covers is unflagged, so payroll collects it
        instead of it waiting on a commission run that will not come.
        """
        Line = self.env['ksw.pay.run.line'].sudo()
        DedLine = self.env['ksw.deduction.line'].sudo()
        for run in self.sudo():
            if run.state != 'approved':
                continue
            totals = run._bas_component_totals()
            for employee in employees:
                earnings = sum(totals.get(employee.id, {}).values())
                line = run.line_ids.filtered(
                    lambda l, emp=employee: l.employee_id == emp)
                if float_compare(line.earnings if line else 0.0, earnings,
                                 precision_digits=2) == 0:
                    continue
                before = line.earnings if line else 0.0
                exported = line.x_bank_exported_date if line else False
                released = DedLine
                if line:
                    payload = json.loads(line.x_unwind_data or '{}')
                    released = DedLine.browse(
                        payload.get('paid_ids') or []).exists()
                    released |= DedLine.browse(
                        [s.get('orig') for s in payload.get('splits') or []]
                    ).exists()
                    line._unwind_loan_offset()
                    if earnings:
                        line.write({'earnings': earnings})
                    else:
                        line.unlink()
                        line = Line
                elif earnings:
                    line = Line.create({
                        'run_id': run.id, 'employee_id': employee.id,
                        'earnings': earnings,
                    })
                if line:
                    line._apply_loan_offset()
                uncovered = released.filtered(
                    lambda l: l.state == 'pending' and l.x_awaiting_commission)
                if uncovered:
                    uncovered.write({'x_awaiting_commission': False})
                run.message_post(
                    body=Markup(
                        '<strong>%(title)s</strong><br/>'
                        '<b>%(l_emp)s</b> %(emp)s<br/>'
                        '<b>%(l_before)s</b> %(before).2f<br/>'
                        '<b>%(l_after)s</b> %(after).2f<br/>'
                        '%(note)s'
                    ) % {
                        'title': _('Register line updated by a vacation '
                                   'settlement'),
                        'l_emp': _('Employee:'),
                        'emp': employee.sudo().display_name,
                        'l_before': _('Earnings before:'), 'before': before,
                        'l_after': _('Earnings after:'), 'after': earnings,
                        'note': _('Loan installments no longer covered by '
                                  'this line go back to payroll: %(n)s',
                                  n=len(uncovered)) if uncovered else '',
                    },
                    subtype_xmlid='mail.mt_note',
                )
                if exported:
                    # The file already made is now wrong for him: say so,
                    # so it is not the one that goes to the bank.
                    run.message_post(
                        body=Markup('<strong>⚠ %(msg)s</strong>') % {
                            'msg': _('%(emp)s changed after the bank file '
                                     'was exported on %(when)s. Export the '
                                     'bank file again before sending it.',
                                     emp=employee.sudo().display_name,
                                     when=format_datetime(self.env, exported)),
                        },
                        subtype_xmlid='mail.mt_note',
                    )
        return True

    @api.model
    def _rounded_component_totals(self, entries):
        """``{employee_id: {component: whole riyals}}``, in catalog order.

        The month is paid in whole riyals: each employee's total for each
        pay type is rounded half-up, and his earnings are the sum of those.
        Rounded per type rather than once per employee because the BAS
        journal posts one line per type — rounding the sum instead would
        leave the lines adding up to a figure the transfer does not pay.
        This is the one place that rule lives; the register and the journal
        both read it, which is what keeps them equal.
        """
        totals = {}
        for entry in entries.sorted(
                lambda e: (e.component_id.sequence, e.component_id.id, e.id)):
            by_component = totals.setdefault(entry.employee_id.id, {})
            by_component[entry.component_id] = (
                by_component.get(entry.component_id, 0.0)
                + (entry.amount or 0.0))
        for by_component in totals.values():
            for component, amount in by_component.items():
                by_component[component] = float_round(
                    amount, precision_digits=0, rounding_method='HALF-UP')
        return totals

    def _held_entries(self, entries=None):
        """The entries this month may not pay — see ``ksw_vacation_hold``."""
        self.ensure_one()
        if entries is None:
            entries = self._all_entries().filtered('employee_id')
        if not entries:
            return self.env['ksw.pay.entry']
        holds = vacation_holds(self.env, entries.employee_id, self.period)
        # A row already paid on a vacation payslip is left out of the
        # register separately — nothing about it is for the GM to release.
        return entries.filtered(
            lambda e: not e.x_vacation_payslip_id
            and hold_blocks(holds.get(e.employee_id.id), e.date))

    def _announce_settled(self, settled):
        """Say whose entries the register skipped because a vacation
        payslip already paid them."""
        self.ensure_one()
        body = Markup(
            '<strong>\u2714 Paid on a vacation payslip</strong><br/>'
            'These entries were left out of the register: they were paid '
            'with the employee\u2019s vacation settlement.<br/>'
        )
        for slip in settled.x_vacation_payslip_id:
            rows = settled.filtered(
                lambda e, s=slip: e.x_vacation_payslip_id == s)
            body += Markup(
                '\u2022 <b>%(name)s</b> \u2014 %(count)s entr%(plural)s, '
                '%(amount).2f: %(slip)s<br/>'
            ) % {
                'name': rows.employee_id[:1].sudo().display_name,
                'count': len(rows),
                'plural': 'y' if len(rows) == 1 else 'ies',
                'amount': sum(rows.mapped('amount')),
                'slip': slip.sudo().number or slip.sudo().name,
            }
        self.sudo().message_post(body=body, subtype_xmlid='mail.mt_note')

    def _announce_held(self, held):
        """Say, on the month, whose entries were left out and why.

        Silence here would be the worst of both: the supervisor typed the
        rows, the GM approved the department, and the money simply would
        not appear in the bank file with nothing anywhere to say so.
        """
        self.ensure_one()
        holds = vacation_holds(self.env, held.employee_id, self.period)
        body = Markup(
            '<strong>\u23f8 Held \u2014 settled on a vacation request'
            '</strong><br/>'
            'These entries were left out of the register. What the employee '
            'had earned was paid on the leave itself, so paying it again '
            'here would pay it twice. If a month was never listed on the '
            'leave, release it under Commissions \u2192 Vacation Releases '
            'and reopen this run.<br/>'
        )
        for employee in held.employee_id:
            rows = held.filtered(
                lambda e, emp=employee: e.employee_id == emp)
            hold = holds.get(employee.id)
            body += Markup(
                '\u2022 <b>%(name)s</b> \u2014 %(count)s entr%(plural)s, '
                '%(amount).2f: %(why)s<br/>'
            ) % {
                'name': employee.sudo().display_name,
                'count': len(rows),
                'plural': 'y' if len(rows) == 1 else 'ies',
                'amount': sum(rows.mapped('amount')),
                'why': hold_reason(self.env, hold) if hold else '',
            }
        self.sudo().message_post(body=body, subtype_xmlid='mail.mt_note')

    def _refresh_register(self):
        """Keep the preview in step. Never touches an approved month."""
        for rec in self:
            if rec.state in LOCKING_STATES:
                continue
            rec.sudo()._build_register(preview=True)
        return True

    def action_open_export_wizard(self):
        self.ensure_one()
        if self.state not in LOCKING_STATES:
            raise UserError(_(
                "The bank file can only be exported once the month has been "
                "approved."))
        self._check_group(
            'KSW_commissions.group_commission_accountant',
            _("Only the Commission Accountant can export the bank file."))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Export Bank File'),
            'res_model': 'ksw.commission.bank.export.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_run_id': self.id},
        }

    def action_open_batches(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Batches'),
            'res_model': 'ksw.pay.batch',
            'view_mode': 'list,form',
            'domain': [('period', '=', self.period)],
            'context': {'default_period': self.period},
        }

    def _group_lines_by_bank_account(self):
        """Group register lines by the employee's paying bank."""
        groups = {}
        bank_model = self.env['res.partner.bank']
        for line in self.line_ids:
            bank = line.bank_account_id or self.x_salary_bank_account_id \
                or bank_model
            groups.setdefault(bank, self.env['ksw.pay.run.line'])
            groups[bank] |= line
        return groups


class KswPayRunLine(models.Model):
    """One employee's payment for the month — generated, not typed."""
    _name = 'ksw.pay.run.line'
    _description = 'KSW Pay Run Line'
    _order = 'run_id, employee_id'

    run_id = fields.Many2one(
        'ksw.pay.run', required=True, ondelete='cascade', index=True,
    )
    period = fields.Date(related='run_id.period', store=True, readonly=True)
    state = fields.Selection(related='run_id.state', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='run_id.currency_id', readonly=True,
    )
    employee_id = fields.Many2one(
        'hr.employee', required=True, ondelete='restrict', index=True,
    )
    department_id = fields.Many2one(
        related='employee_id.department_id', store=True, readonly=True,
    )

    earnings = fields.Monetary(
        readonly=True, help='Sum of every approved entry for this employee.',
    )
    loan_offset = fields.Monetary(
        help='Parked loan installments taken out of this payment. Before the '
             'month is approved this is an estimate of what would be '
             'settled; approval settles it for real, and the accountant may '
             'still correct it before the bank file goes out.',
    )
    is_preview = fields.Boolean(
        compute='_compute_is_preview',
        help='True while the month is still open — the figures are a '
             'projection, not a settlement.',
    )
    # Stored, and the reason the preview is safe to run on any open month.
    # A register line can also be a settled figure — carried over by the
    # migration from the commission sheets that predate this app, or left
    # behind by a reopened month. The preview builder owns only the lines it
    # created itself and will not touch, still less delete, the others.
    x_preview_generated = fields.Boolean(
        default=False, readonly=True, copy=False,
        string='Generated by the Preview',
    )
    net_payable = fields.Monetary(compute='_compute_net', store=True)
    bank_account_id = fields.Many2one(
        'res.partner.bank', compute='_compute_bank_account', store=True,
        readonly=False,
    )
    x_unwind_data = fields.Text(readonly=True, copy=False)
    # Audit only: when a bank file carrying this line was last produced.
    # NOT a payment — only the run's Paid state is (``_months_paid_to``).
    x_bank_exported_date = fields.Datetime(
        string='Bank File Exported On', readonly=True, copy=False)
    x_bank_exported_by = fields.Many2one(
        'res.users', string='Bank File Exported By', readonly=True,
        copy=False)

    entry_ids = fields.Many2many(
        'ksw.pay.entry', compute='_compute_entry_ids', string='Entries',
    )

    _unique_employee_per_run = models.Constraint(
        'UNIQUE(run_id, employee_id)',
        'An employee can only appear once in a pay run.')

    @api.depends('state')
    def _compute_is_preview(self):
        for rec in self:
            rec.is_preview = rec.state not in LOCKING_STATES

    def _export_submissions(self):
        """``{(run_id, employee_id): ksw.pay.submission}`` — whose handover
        each person was paid through.

        The department handovers in the order the run's *By Department*
        page lists them (the submission model's own ``_order``); a person
        whose entries sit in two of them belongs to the first.
        """
        owner = {}
        for run in self.run_id:
            for submission in run.sudo().submission_ids:
                for employee in submission.batch_ids.entry_ids.employee_id:
                    owner.setdefault((run.id, employee.id), submission)
        return owner

    def _export_sorted(self):
        """The one order every file of the month lists people in.

        The bank Excel, the bank text files and the BAS journal are read
        side by side, so they must agree row for row: department by
        department, in the order of the run's *By Department* page, and by
        name inside each — trimmed and case-blind, because names here carry
        leading spaces and mixed case and a plain sort files " AHMED" before
        "AAT" and "abdullah" after "ZAHID" — with the id to settle ties.
        Anyone not traceable to a handover (a register line carried over
        by hand) comes last.
        """
        owner = self._export_submissions()
        position = {}
        for run in self.run_id:
            for pos, submission in enumerate(run.sudo().submission_ids):
                position[submission.id] = pos

        def key(line):
            submission = owner.get((line.run_id.id, line.employee_id.id))
            return (
                position.get(submission.id, len(position))
                if submission else len(position),
                (line.employee_id.sudo().name or '').strip().casefold(),
                line.id,
            )
        return self.sorted(key)

    @api.depends('earnings', 'loan_offset')
    def _compute_net(self):
        for rec in self:
            rec.net_payable = (rec.earnings or 0.0) - (rec.loan_offset or 0.0)

    @api.depends('employee_id')
    def _compute_bank_account(self):
        for rec in self:
            emp = rec.employee_id.sudo()
            rec.bank_account_id = getattr(
                emp, 'x_salary_bank_account_id', False) or False

    @api.depends('employee_id', 'period')
    def _compute_entry_ids(self):
        Entry = self.env['ksw.pay.entry']
        for rec in self:
            rec.entry_ids = Entry.search([
                ('employee_id', '=', rec.employee_id.id),
                ('period', '=', rec.period),
            ])

    @api.depends('employee_id', 'run_id')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = '%s — %s' % (
                rec.employee_id.display_name or '',
                rec.run_id.display_name or '')

    def action_open_entries(self):
        """The justification behind the figure: every entry that made it."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Entries for %(name)s',
                      name=self.employee_id.display_name),
            'res_model': 'ksw.pay.entry',
            'view_mode': 'list,form',
            'domain': [('employee_id', '=', self.employee_id.id),
                       ('period', '=', self.period)],
        }

    # ------------------------------------------------------------------
    # KSW_deduction integration — settle parked installments
    # ------------------------------------------------------------------
    def _pending_loan_total(self):
        """What this employee has parked as 'Awaiting Commission'."""
        self.ensure_one()
        total, _lines = self.env['ksw.deduction'].sudo(
        )._get_pending_commission_lines_for_period(
            self.employee_id, self.period)
        return total

    def _auto_flag_priority_installments(self):
        """Park this period's pending installments for every employee who
        has 'Settle Deductions from Commission First' enabled, so
        ``_apply_loan_offset`` settles them out of commission before
        anything reaches payroll.

        Only lines not already parked are touched. Returns the ids this
        call flagged, so the caller can tell them apart afterwards from
        lines an accountant had already parked manually — those must keep
        waiting for a future commission run if commission falls short,
        exactly as today, while ours must fall straight through to this
        month's payslip instead.
        """
        Line = self.env['ksw.deduction.line'].sudo()
        Ded = self.env['ksw.deduction'].sudo()
        auto_ids = []
        for rec in self:
            if not rec.employee_id.x_deduct_commission_priority:
                continue
            year, month = Ded._period_to_year_month(rec.period)
            lines = Line.search([
                ('employee_id', '=', rec.employee_id.id),
                ('state', '=', 'pending'),
                ('year', '=', year), ('month', '=', month),
                ('x_awaiting_commission', '=', False),
                ('deduction_id.state', '=', 'active'),
            ])
            if lines:
                lines.write({'x_awaiting_commission': True})
                auto_ids += lines.ids
        return auto_ids

    def _unflag_uncovered_auto_installments(self, auto_ids):
        """Undo the flag on any auto-flagged line ``_apply_loan_offset``
        did not fully consume (still pending afterward), so it falls into
        this month's payslip immediately instead of waiting on a future
        commission run.
        """
        if not auto_ids:
            return
        leftover = self.env['ksw.deduction.line'].sudo().browse(
            auto_ids).exists().filtered(
                lambda l: l.state == 'pending' and l.x_awaiting_commission)
        if leftover:
            leftover.write({'x_awaiting_commission': False})

    def _apply_loan_offset(self):
        """Settle parked installments out of the commission payment.

        Ported unchanged in behaviour from the old commission sheet: FIFO
        over the employee's pending ``x_awaiting_commission`` installments,
        splitting the last one when the payment only partly covers it, and
        snapshotting enough to undo it.
        """
        Ded = self.env['ksw.deduction'].sudo()
        Line = self.env['ksw.deduction.line'].sudo()
        for rec in self:
            available = rec.earnings or 0.0
            total, lines = Ded._get_pending_commission_lines_for_period(
                rec.employee_id, rec.period)
            # Whole riyals, like the earnings: a fractional installment is
            # settled down to the riyal and the rest stays pending, so the
            # transfer is never a fraction.
            amount = math.floor(min(available, total) + 1e-6)
            if amount <= 0.0:
                rec.loan_offset = 0.0
                rec.x_unwind_data = False
                continue

            paid_ids, splits = [], []
            remaining = amount
            touched = self.env['ksw.deduction']
            # Settling out of commission is a third collection route
            # alongside payroll and manual payment, and like them it dates
            # its credit on the Statement of Account on the day it happened:
            # the day the month was approved, which is when the installment
            # is taken out of the commission. Read from `approved_date`
            # rather than today so a later re-application of the offset
            # (`_resync_vacation_line`) keeps the original date.
            #
            # Not the end of the commission period, which is what this used
            # to stamp: a loan disbursed in September whose August
            # installment the August run settled showed the collection on
            # 31 Aug, before its own charge.
            approved = rec.run_id.approved_date
            settled_on = (
                fields.Date.context_today(rec, timestamp=approved)
                if approved else fields.Date.context_today(rec)
            )

            for line in lines:
                if remaining <= 1e-6:
                    break
                line_amt = line.amount or 0.0
                if line_amt <= remaining + 1e-6:
                    line.with_context(
                        _skip_installment_total_check=True,
                    ).write({
                        'state': 'paid',
                        'x_paid_via_pay_run_line_id': rec.id,
                        'x_awaiting_commission': False,
                        'x_settlement_date': settled_on,
                    })
                    paid_ids.append(line.id)
                    touched |= line.deduction_id
                    remaining -= line_amt
                else:
                    take = remaining
                    new_line = Line.with_context(
                        _ksw_auto_generating=True,
                        _skip_installment_total_check=True,
                    ).create({
                        'deduction_id': line.deduction_id.id,
                        'sequence': line.sequence,
                        'year': line.year,
                        'month': line.month,
                        'amount': take,
                        'state': 'paid',
                        'is_manual': False,
                        'x_awaiting_commission': False,
                        'x_paid_via_pay_run_line_id': rec.id,
                        'x_original_amount': take,
                        'x_settlement_date': settled_on,
                    })
                    line.with_context(
                        _skip_installment_total_check=True,
                    ).write({'amount': line_amt - take})
                    splits.append({'orig': line.id, 'new': new_line.id,
                                   'taken': take})
                    touched |= line.deduction_id
                    remaining = 0.0

            for ded in touched:
                ded._validate_installments_total()

            rec.loan_offset = amount
            rec.x_unwind_data = json.dumps(
                {'paid_ids': paid_ids, 'splits': splits})

    def _unwind_loan_offset(self):
        """Give the settled installments back."""
        Line = self.env['ksw.deduction.line'].sudo()
        for rec in self:
            if not rec.x_unwind_data:
                continue
            try:
                payload = json.loads(rec.x_unwind_data)
            except Exception:
                raise UserError(_(
                    "Cannot reopen: the unwind snapshot for %(name)s is "
                    "corrupted. Manual cleanup required.",
                    name=rec.display_name))
            touched = self.env['ksw.deduction']

            for line in Line.browse(payload.get('paid_ids') or []).exists():
                line.with_context(
                    _skip_installment_total_check=True,
                ).write({
                    'state': 'pending',
                    'x_paid_via_pay_run_line_id': False,
                    'x_awaiting_commission': True,
                    'x_settlement_date': False,
                })
                touched |= line.deduction_id

            for split in payload.get('splits') or []:
                orig = Line.browse(split.get('orig')).exists()
                new = Line.browse(split.get('new')).exists()
                if orig:
                    orig.with_context(
                        _skip_installment_total_check=True,
                    ).write({'amount': (orig.amount or 0.0)
                             + (split.get('taken') or 0.0)})
                    touched |= orig.deduction_id
                if new:
                    new.with_context(
                        _skip_installment_total_check=True).unlink()

            for ded in touched:
                ded._validate_installments_total()
            rec.x_unwind_data = False
