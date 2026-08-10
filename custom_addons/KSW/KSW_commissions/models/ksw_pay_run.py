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

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .ksw_commission_lock import LOCKING_STATES

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
    x_can_reopen = fields.Boolean(compute='_compute_permissions')
    x_can_export = fields.Boolean(compute='_compute_permissions')

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
    @api.depends('submission_ids.state')
    def _compute_visible_submissions(self):
        Submission = self.env['ksw.pay.submission']
        allowed = self.env['ksw.pay.batch']._allowed_departments()
        is_officer = self.env.user.has_group(
            'KSW_commissions.group_commission_officer')
        for rec in self:
            visible = Submission.search([('run_id', '=', rec.id)])
            rec.x_visible_submission_ids = visible
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
    @api.depends('state', 'submitted_department_count')
    def _compute_permissions(self):
        user = self.env.user
        is_supervisor = user.has_group(
            'KSW_commissions.group_commission_supervisor')
        is_accountant = user.has_group(
            'KSW_commissions.group_commission_accountant')
        is_gm = user.has_group('KSW_commissions.group_commission_gm')
        for rec in self:
            rec.x_can_submit = is_supervisor and rec.state == 'open'
            # The month may be approved as soon as at least one department
            # has handed over. Waiting for every department would let one
            # late supervisor hold up everybody else's pay.
            rec.x_can_approve = (
                is_gm and rec.state not in LOCKING_STATES
                and rec.submitted_department_count > 0)
            rec.x_can_reopen = is_gm and rec.state in LOCKING_STATES
            rec.x_can_export = is_accountant and rec.state in LOCKING_STATES

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

    def _payable_batches(self):
        """The batches whose entries are actually going to be paid.

        A batch counts only once its **department** has been handed over —
        a submitted batch on its own is just a supervisor who has finished
        typing, not a department declaring itself complete.
        """
        self.ensure_one()
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
        mine.action_submit()
        return self._notify_toast(_(
            "%(count)s department(s) submitted to the General Manager.",
            count=len(mine)))

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

    def _notify_toast(self, message):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': _('Monthly Pay Run'), 'message': message,
                       'type': 'success', 'sticky': False},
        }

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

    def action_approve(self):
        """submitted → approved. Builds the register and locks the month."""
        self._check_group(
            'KSW_commissions.group_commission_gm',
            _("Only the General Manager can approve the month."))
        for rec in self:
            if rec.state in LOCKING_STATES:
                raise UserError(_(
                    "%(name)s has already been approved.",
                    name=rec.display_name))
            submitted = rec.submission_ids.filtered(
                lambda s: s.state == 'submitted')
            if not submitted:
                raise UserError(_(
                    "No department has submitted its commissions for "
                    "%(period)s, so there is nothing to approve.",
                    period=rec.display_name))
            # Only what was handed over gets paid. A department that has not
            # submitted keeps its work in draft rather than being swept into
            # a month it never declared itself ready for.
            left_out = rec.pending_department_names
            submitted.mapped('batch_ids').sudo().write({'state': 'approved'})
            submitted.sudo().write({'state': 'approved'})
            rec._build_register()
            rec.write({
                'state': 'approved',
                'approved_by': self.env.uid,
                'approved_date': fields.Datetime.now(),
            })
            rec.line_ids.sudo()._apply_loan_offset()
            body = Markup(
                '<strong>✅ Approved</strong><br/>'
                '<b>Departments:</b> %(depts)s<br/>'
                '<b>Employees:</b> %(count)s<br/>'
                '<b>Earnings:</b> %(earn).2f<br/>'
                '<b>Loans settled:</b> %(loans).2f<br/>'
                '<b>Payable:</b> %(pay).2f<br/>'
                '<i>%(period)s is now locked.</i>'
            ) % {'depts': len(submitted), 'count': rec.employee_count,
                 'earn': rec.total_earnings or 0.0,
                 'loans': rec.total_loan_offset or 0.0,
                 'pay': rec.total_payable or 0.0,
                 'period': rec.display_name}
            if left_out:
                body += Markup(
                    '<br/><b>⚠ Not included</b> (never submitted): '
                    '%(names)s'
                ) % {'names': left_out}
            rec.sudo().message_post(body=body, subtype_xmlid='mail.mt_note')
        return True

    def action_return_to_supervisors(self):
        """submitted → open, so corrections can be made."""
        self._check_group(
            'KSW_commissions.group_commission_gm',
            _("Only the General Manager can return the month."))
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
        """Unlock an approved month. GM only — they signed it off."""
        self._check_group(
            'KSW_commissions.group_commission_gm',
            _("Only the General Manager can reopen an approved month."))
        for rec in self:
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
            reopened = rec.submission_ids.filtered(
                lambda s: s.state == 'approved')
            reopened.mapped('batch_ids').filtered(
                lambda b: b.state == 'approved'
            ).sudo().write({'state': 'submitted'})
            reopened.sudo().write({'state': 'submitted'})
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

        totals = {}
        for batch in self._payable_batches():
            for entry in batch.sudo().entry_ids:
                if not entry.employee_id:
                    continue
                totals.setdefault(entry.employee_id.id, 0.0)
                totals[entry.employee_id.id] += entry.amount or 0.0

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
                line.loan_offset = min(amount, line._pending_loan_total())
            else:
                # From approval on, this is the settlement itself.
                line.x_preview_generated = False
        return self.line_ids

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
            amount = min(available, total)
            if amount <= 0.0:
                rec.loan_offset = 0.0
                rec.x_unwind_data = False
                continue

            paid_ids, splits = [], []
            remaining = amount
            touched = self.env['ksw.deduction']

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
