"""KSW Pay Sub-Batch — some employees' month, handed over ahead of the rest.

A department's month is one handover (``ksw.pay.submission``), and handing it
over freezes every batch in it. That is right at the end of the month and
wrong in the middle of it: an employee leaving on vacation on the 12th needs
what he has earned so far approved *now* — the vacation settlement only pays
entries the General Manager has approved — while the supervisor keeps typing
for everybody else.

A sub-batch is that early handover. It names employees, not rows: it takes
every draft entry those employees have in the department's batches for the
month (overtime, meals, Fridays — all of it), and anything typed for them
while the sub-batch is still open joins it. It has its own lock, separate
from the batches it cuts across:

* submitting it locks **only its rows** — the batches stay open;
* the GM approves it, or returns some of its employees and approves the
  rest (``ksw.pay.gm.review.wizard``);
* approved rows are frozen, and a batch holding them cannot be deleted;
* the GM may reopen an approved row until it is paid — on a vacation
  payslip, or in a month marked Paid — and never after.

It is still part of the department's month: the approved rows are paid by
the month's register (or by the vacation payslip, which the register then
skips), and when the supervisor finally submits the department, he is asked
whether he means the whole of it or only his open sub-batches.
"""
import calendar

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .ksw_commission_lock import LOCKING_STATES, check_period_unlocked

SUB_BATCH_STATES = [
    ('draft', 'Being Prepared'),
    ('submitted', 'Submitted to GM'),
    ('returned', 'Returned for Correction'),
    ('approved', 'Approved'),
]

#: A sub-batch in one of these still owns its employees: a second one for
#: the same person would split his rows between two handovers.
OPEN_STATES = ('draft', 'returned', 'submitted')


class KswPaySubBatch(models.Model):
    _name = 'ksw.pay.sub.batch'
    _description = 'KSW Pay Sub-Batch (early handover of some employees)'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'period desc, id desc'

    name = fields.Char(readonly=True, default='New', copy=False)
    submission_id = fields.Many2one(
        'ksw.pay.submission', string='Department Month', required=True,
        ondelete='restrict', index=True, readonly=True,
    )
    run_id = fields.Many2one(
        related='submission_id.run_id', store=True, readonly=True)
    period = fields.Date(
        related='submission_id.period', store=True, readonly=True, index=True)
    department_id = fields.Many2one(
        related='submission_id.department_id', store=True, readonly=True)
    # Stored: the GM's record rule filters on it.
    gm_id = fields.Many2one(
        related='submission_id.gm_id', store=True, readonly=True,
        string='General Manager')
    currency_id = fields.Many2one(
        related='submission_id.currency_id', readonly=True)

    # Empty on every sub-batch made since Oct 2026: one sub-batch is one
    # department, days and reason, across every component (Hafiz's Friday
    # Work, Location Allowance and Other are one handover, not three). Set
    # only on the per-component sub-batches of 19.0.4.26.0 – 4.38.0, which
    # keep drawing from that one batch.
    batch_id = fields.Many2one(
        'ksw.pay.batch', string='Component Batch', readonly=True,
        index=True, ondelete='restrict')
    component_id = fields.Many2one(
        related='batch_id.component_id', store=True, readonly=True,
        string='Component')
    employee_ids = fields.Many2many(
        'hr.employee', 'ksw_pay_sub_batch_employee_rel', 'sub_batch_id',
        'employee_id', string='Employees',
        domain="[('id', 'in', allowed_employee_ids)]",
    )
    # Whoever has a draft row in this department's month — the only people
    # there is anything to hand over for. compute_sudo for the reason the
    # batch's own picker has it (Odoo 19 Pitfalls #34).
    allowed_employee_ids = fields.Many2many(
        'hr.employee', compute='_compute_allowed_employees',
        compute_sudo=True)
    # For the GM and the accountant: his hr.employee rule covers his
    # departments, not a supervisor's reporting chain in another one, so a
    # plain many2many of employees could fail to render for him.
    employee_names = fields.Char(
        compute='_compute_employee_names', compute_sudo=True,
        string='Employees ')
    entry_ids = fields.One2many(
        'ksw.pay.entry', 'x_sub_batch_id', string='Entries', readonly=True)
    # The days this handover is about. A row with a day of its own — an
    # overtime date, the days an imported trip figure covers — comes into
    # the sub-batch only when it falls inside; the Driver Trips import run
    # from here fetches exactly these days. A monthly allowance has no day
    # and comes with its employee regardless.
    date_from = fields.Date(
        string='From', copy=False, tracking=True,
        help='First day this sub-batch covers. Defaults to the 1st of the '
             'month.')
    date_to = fields.Date(
        string='To', copy=False, tracking=True,
        help='Last day this sub-batch covers. Defaults to today, or the '
             'end of the month for a month already over.')
    # A returned Driver Trips sub-batch cannot be corrected by hand (the
    # component is import-only): re-importing its drivers for its days is
    # the correction.
    x_can_reimport = fields.Boolean(compute='_compute_permissions')
    x_overlap_count = fields.Integer(
        compute='_compute_totals', string='Overlapping Rows')
    note = fields.Text(
        string='Why Early',
        help='Why these employees cannot wait for the rest of the month — '
             'a vacation from the 12th, a resignation, ...')

    state = fields.Selection(
        SUB_BATCH_STATES, default='draft', required=True, copy=False,
        tracking=True, readonly=True)
    submitted_by = fields.Many2one('res.users', readonly=True, copy=False)
    submitted_date = fields.Datetime(readonly=True, copy=False)
    approved_by = fields.Many2one('res.users', readonly=True, copy=False)
    approved_date = fields.Datetime(readonly=True, copy=False)
    return_reason = fields.Text(readonly=True, copy=False)

    entry_count = fields.Integer(compute='_compute_totals')
    pending_count = fields.Integer(compute='_compute_totals')
    approved_count = fields.Integer(compute='_compute_totals')
    total_amount = fields.Monetary(compute='_compute_totals')

    x_can_edit = fields.Boolean(compute='_compute_permissions')
    x_can_submit = fields.Boolean(compute='_compute_permissions')
    x_can_review = fields.Boolean(compute='_compute_permissions')
    x_can_reopen = fields.Boolean(compute='_compute_permissions')

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends('submission_id', 'batch_id')
    def _compute_allowed_employees(self):
        Entry = self.env['ksw.pay.entry']
        for rec in self:
            rec.allowed_employee_ids = Entry.search(
                rec._row_domain() + [('state', '=', 'draft')]
            ).employee_id if rec.submission_id else False

    def _row_domain(self):
        """The rows this sub-batch draws from: its component batch, or —
        for a sub-batch from before it had one — the whole month."""
        self.ensure_one()
        if self.batch_id:
            return [('batch_id', '=', self.batch_id.id)]
        return [('batch_id.submission_id', '=', self.submission_id.id)]

    @api.depends('employee_ids')
    def _compute_employee_names(self):
        for rec in self:
            rec.employee_names = ', '.join(rec.employee_ids.mapped('name'))

    @api.depends('entry_ids.state', 'entry_ids.amount')
    def _compute_totals(self):
        for rec in self:
            entries = rec.sudo().entry_ids
            rec.entry_count = len(entries)
            rec.pending_count = len(
                entries.filtered(lambda e: e.state == 'submitted'))
            rec.approved_count = len(
                entries.filtered(lambda e: e.state == 'approved'))
            rec.total_amount = sum(entries.mapped('amount'))
            rec.x_overlap_count = len(
                entries.filtered(lambda e: e.x_overlap_note))

    @api.depends_context('uid')
    @api.depends('state', 'gm_id', 'run_id.state', 'entry_ids.state')
    def _compute_permissions(self):
        user = self.env.user
        is_supervisor = user.has_group(
            'KSW_commissions.group_commission_supervisor')
        for rec in self:
            month_open = rec.run_id.state not in LOCKING_STATES
            is_my_gm = bool(rec.gm_id) and rec.gm_id == user
            open_ = rec.state in ('draft', 'returned')
            rec.x_can_edit = is_supervisor and month_open and open_
            rec.x_can_submit = rec.x_can_edit
            rec.x_can_review = is_my_gm and month_open and bool(
                rec.pending_count)
            rec.x_can_reopen = is_my_gm and month_open and bool(
                rec.approved_count)
            rec.x_can_reimport = rec.x_can_edit and rec.state == 'returned' \
                and bool(rec._importer_batches())

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        Seq = self.env['ir.sequence']
        employees = []
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == 'New':
                vals['name'] = Seq.next_by_code('ksw.pay.sub.batch') or 'New'
            employees.append(vals.pop('employee_ids', None))
            if vals.get('batch_id'):
                batch = self.env['ksw.pay.batch'].sudo().browse(
                    vals['batch_id'])
                if not batch.submission_id:
                    batch._ensure_submission()
                vals.setdefault('submission_id', batch.submission_id.id)
            if not vals.get('submission_id'):
                raise UserError(_(
                    "A sub-batch belongs to a department's month: press "
                    "Submit to GM on it, or Send Early… on one of its "
                    "batches."))
            self._default_range(vals)
        records = super().create(vals_list)
        for rec, commands in zip(records, employees):
            rec._check_can_prepare(_("Creating a sub-batch"))
            if commands:
                rec._write_employees(commands)
        records._collect_entries()
        return records

    def write(self, vals):
        if self.env.context.get('ksw_sub_batch_employees'):
            return super().write(vals)
        commands = vals.pop('employee_ids', None)
        range_changed = bool({'date_from', 'date_to'} & set(vals))
        if range_changed and not self.env.su:
            for rec in self:
                rec._check_can_prepare(_("Changing its dates"))
        res = super().write(vals) if vals else True
        if commands is not None:
            for rec in self:
                if not self.env.su:
                    rec._check_can_prepare(_("Changing its employees"))
                rec._write_employees(commands)
        if commands is not None or range_changed:
            self._collect_entries()
        return res

    @api.model
    def _default_range(self, vals):
        """1st of the month to today — or to the month's end when today
        is not in it. Filled server-side so a sub-batch made over RPC or by
        a script is never without days."""
        if vals.get('date_from') and vals.get('date_to'):
            return
        submission = self.env['ksw.pay.submission'].sudo().browse(
            vals.get('submission_id'))
        if not submission.period:
            return
        start = submission.period.replace(day=1)
        end = start.replace(
            day=calendar.monthrange(start.year, start.month)[1])
        today = fields.Date.context_today(self)
        vals.setdefault('date_from', start)
        vals.setdefault('date_to', today if start <= today <= end else end)

    @api.onchange('submission_id')
    def _onchange_submission_range(self):
        if self.submission_id and not (self.date_from and self.date_to):
            vals = {'submission_id': self.submission_id.id}
            self._default_range(vals)
            self.date_from = vals.get('date_from')
            self.date_to = vals.get('date_to')

    @api.constrains('date_from', 'date_to', 'period')
    def _check_range(self):
        for rec in self:
            if not rec.period:
                continue
            if not (rec.date_from and rec.date_to):
                raise ValidationError(_(
                    "%(name)s needs the days it covers (From and To).",
                    name=rec.name))
            start = rec.period.replace(day=1)
            end = start.replace(
                day=calendar.monthrange(start.year, start.month)[1])
            if not (start <= rec.date_from <= rec.date_to <= end):
                raise ValidationError(_(
                    "The days of %(name)s must run forward and stay inside "
                    "%(month)s.", name=rec.name,
                    month=start.strftime('%B %Y')))

    def _write_employees(self, commands):
        """Set the employees through sudo(), after the caller's authority
        was checked (_check_can_prepare) and the choice is proven to be his.

        Writing a many2many checks read access on hr.employee, which a
        supervisor has only through KSW_base_security's subordinate group —
        and has not at all without it (Odoo 19 Pitfalls #34). Whether he may
        name these people is this module's question, answered below: only
        someone with a row in this department's month.
        """
        self.ensure_one()
        self.sudo().with_context(ksw_sub_batch_employees=True).write(
            {'employee_ids': commands})
        known = self.env['ksw.pay.entry'].sudo().search(
            self._row_domain()).employee_id
        stray = self.sudo().employee_ids - known
        if stray:
            raise UserError(_(
                "%(who)s has no entry in %(scope)s, so there is nothing to "
                "hand over for them.",
                who=', '.join(stray.mapped('name')),
                scope=self.batch_id.display_name
                or self.submission_id.display_name))

    def unlink(self):
        for rec in self:
            if rec.state != 'draft' or rec.sudo().entry_ids.filtered(
                    lambda e: e.state != 'draft'):
                raise UserError(_(
                    "%(name)s has been handed to the General Manager and "
                    "cannot be deleted.", name=rec.name))
        return super().unlink()

    @api.constrains('employee_ids', 'submission_id', 'state')
    def _check_one_open_per_employee(self):
        """An employee's open rows belong to one handover at a time."""
        for rec in self:
            if rec.state not in OPEN_STATES:
                continue
            # A department-wide sub-batch takes every component, so it clashes
            # with any open one; an older per-component one only with its own
            # batch's and the department-wide ones.
            domain = [
                ('id', '!=', rec.id),
                ('submission_id', '=', rec.submission_id.id),
                ('state', 'in', OPEN_STATES),
                ('employee_ids', 'in', rec.employee_ids.ids),
            ]
            if rec.batch_id:
                domain.append(('batch_id', 'in', [rec.batch_id.id, False]))
            clash = self.sudo().search(domain, limit=1)
            if clash:
                who = rec.employee_ids & clash.employee_ids
                raise ValidationError(_(
                    "%(who)s is already in %(other)s, which is still open. "
                    "Add the rows there, or wait until it is approved.",
                    who=', '.join(who.sudo().mapped('name')),
                    other=clash.name))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _check_can_prepare(self, what):
        """The supervisor's side: his department, an open month, and a
        department that has not been handed over as a whole already."""
        self.ensure_one()
        self.submission_id._check_mine()
        self._check_month_open(what)
        check_period_unlocked(self.env, self.period, what,
                              gm=self.submission_id.sudo().run_id.gm_id)
        if self.submission_id.state in ('submitted', 'approved'):
            raise UserError(_(
                "%(scope)s has already been handed to the General Manager as "
                "a whole — there is nothing left to send early.",
                scope=self.submission_id.display_name))
        if self.state not in ('draft', 'returned'):
            raise UserError(_(
                "%(name)s is with the General Manager. %(what)s is not "
                "possible until he returns it.", name=self.name, what=what))

    def _check_month_open(self, what):
        """Not GM-exempt, unlike check_period_unlocked: once the month's
        register is built, a row approved or reopened after it is a row the
        bank file silently disagrees with."""
        for rec in self:
            if rec.run_id.state in LOCKING_STATES:
                raise UserError(_(
                    "%(month)s has already been finalised. %(what)s is no "
                    "longer possible — the month has to be reopened first.",
                    month=rec.run_id.display_name, what=what))

    def _check_gm(self):
        """Only this department's own General Manager decides it."""
        if self.env.su:
            return
        for rec in self:
            if not rec.gm_id or rec.gm_id != self.env.user:
                raise UserError(_(
                    "Only %(gm)s, the General Manager of %(scope)s, can act "
                    "on %(name)s.", gm=rec.gm_id.name or _('the department GM'),
                    scope=rec.department_id.name or '', name=rec.name))

    def _importer_batches(self):
        """The imported components (Driver Trips) this sub-batch can fill:
        its own batch, or the department's open importer batches."""
        self.ensure_one()
        if self.batch_id:
            return self.batch_id.filtered('component_id.importer')
        return self.sudo().submission_id.batch_ids.filtered(
            lambda b: b.state == 'draft' and b.component_id.importer)

    def _import_trips(self, drivers):
        """Fetch ``drivers``' trips for this sub-batch's days, component by
        component. An empty BAS answer for one component is reported, not
        raised: it is no reason to refuse the driver's typed rows.
        Returns the messages for whatever could not be imported."""
        self.ensure_one()
        Wizard = self.env['ksw.pay.submit.wizard']
        skipped = []
        for batch in self._importer_batches():
            mine = drivers & Wizard._drivers(batch)
            if not mine:
                continue
            batch = batch.with_env(self.env).with_context(
                ksw_pay_importing=True)
            try:
                with self.env.cr.savepoint():
                    result = batch._import_bas_trips(
                        employees=mine, window=(self.date_from, self.date_to),
                        sub_batch=self)
            except UserError as err:
                skipped.append('%s: %s' % (batch.component_id.name,
                                           err.args[0]))
                continue
            got = batch.sudo().entry_ids.filtered(
                lambda e: e.employee_id in mine and e.state == 'draft'
                and (e.x_window_from, e.x_window_to)
                == (self.date_from, self.date_to))
            if not got:
                skipped.append('%s: %s' % (
                    batch.component_id.name,
                    result['params']['message']))
        return skipped

    def _add_employees(self, employees):
        """Put ``employees`` in this sub-batch with everything they have in
        its days: their typed rows, and their trips imported for the days.

        Only people with something to send are added. Returns messages for
        what could not be imported."""
        self.ensure_one()
        self._check_can_prepare(_("Adding employees"))
        if self.state != 'draft':
            raise UserError(_(
                "%(name)s has already been submitted, so nobody can be "
                "added to it. Start a new sub-batch with its own days and "
                "reason.", name=self.name))
        picked = employees.sudo() & self.env['ksw.pay.submit.wizard']._pool(
            self.submission_id, self.date_from, self.date_to)
        if not picked:
            return []
        skipped = self._import_trips(picked)
        has_rows = self.env['ksw.pay.entry'].sudo().search(
            self._row_domain() + [
                ('employee_id', 'in', picked.ids), ('state', '=', 'draft'),
                ('x_sub_batch_id', 'in', [False, self.id]),
            ]).filtered(
            lambda e: e._within(self.date_from, self.date_to)).employee_id
        if has_rows:
            self.write({'employee_ids': [(4, e.id) for e in has_rows]})
        return skipped

    def action_add_employees(self):
        """Add Employees…: the Submit to GM dialog, on this sub-batch."""
        self.ensure_one()
        self._check_can_prepare(_("Adding employees"))
        if self.state != 'draft':
            raise UserError(_(
                "%(name)s has already been submitted, so nobody can be "
                "added to it. Start a new sub-batch with its own days and "
                "reason.", name=self.name))
        return self.env['ksw.pay.submit.wizard']._open_for(
            self.submission_id, sub_batch=self)

    def _collect_entries(self):
        """Tag every draft row of these employees in the department's month,
        and let go of the rows of anyone taken off the list."""
        Entry = self.env['ksw.pay.entry'].sudo()
        for rec in self.sudo():
            if rec.state not in ('draft', 'returned'):
                continue
            rows = Entry.search(rec._row_domain() + [
                ('employee_id', 'in', rec.employee_ids.ids),
                ('state', '=', 'draft'),
                '|', ('x_sub_batch_id', '=', False),
                ('x_sub_batch_id', '=', rec.id),
            ]).filtered(lambda e: e._within(rec.date_from, rec.date_to))
            (rows - rec.entry_ids).write({'x_sub_batch_id': rec.id})
            rec.entry_ids.filtered(
                lambda e: e.state == 'draft' and e not in rows
            ).write({'x_sub_batch_id': False})
        return True

    def _sync_state_from_entries(self):
        """The sub-batch reads what its rows say, once it has been handed
        over; before that it is simply being prepared."""
        for rec in self.sudo():
            handed = rec.submitted_date or rec.submission_id.state not in (
                False, 'draft')
            states = set(rec.entry_ids.mapped('state'))
            if not handed or not states:
                continue
            if 'submitted' in states:
                target = 'submitted'
            elif 'draft' in states:
                target = 'returned'
            else:
                target = 'approved'
            if target != rec.state:
                rec.write({'state': target})
        return True

    def _gm_partners(self):
        return self.mapped('gm_id').partner_id

    def _supervisor_partners(self):
        users = self.mapped('submitted_by') | self.mapped('create_uid') \
            | self.mapped('submission_id.responsible_id')
        return users.partner_id

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def action_submit(self):
        """Hand these employees' month so far to the General Manager.

        Locks only their rows. The batches they sit in stay open, so the
        supervisor keeps recording for everybody else — and for them too:
        anything typed afterwards is a new row, with the rest of the month.
        """
        for rec in self:
            rec._check_can_prepare(_("Submitting it"))
            rec._collect_entries()
            rows = rec.sudo().entry_ids.filtered(lambda e: e.state == 'draft')
            if not rows:
                raise UserError(_(
                    "%(name)s has nothing to submit: none of its employees "
                    "has a draft entry in %(scope)s.",
                    name=rec.name, scope=rec.submission_id.display_name))
            # Held rows go along and the register leaves them out — see
            # ksw.pay.batch._check_no_held_entries.
            held = rows._held_for_submit()
            if held:
                rec.sudo().message_post(body=held._held_submit_note(),
                                        subtype_xmlid='mail.mt_note')
            resubmitted = rec.state == 'returned'
            rows._wf_submit()
            rec.sudo().write({
                'state': 'submitted',
                'submitted_by': self.env.uid,
                'submitted_date': fields.Datetime.now(),
                'return_reason': False,
            })
            rows._sync_containers()
            rec.sudo().message_post(
                body=Markup(
                    '<strong>%(title)s</strong><br/>'
                    '<b>%(l_dept)s</b> %(dept)s<br/>'
                    '<b>%(l_emp)s</b> %(emp)s<br/>'
                    '<b>%(l_rows)s</b> %(rows)s · <b>%(l_total)s</b> '
                    '%(total).2f<br/>'
                    '<b>%(l_why)s</b> %(why)s'
                ) % {
                    'title': _('🔁 Sub-batch resubmitted') if resubmitted
                    else _('📤 Sub-batch submitted early'),
                    'l_dept': _('Department:'),
                    'dept': rec.submission_id.display_name,
                    'l_emp': _('Employees:'), 'emp': rec.employee_names,
                    'l_rows': _('Entries:'), 'rows': len(rows),
                    'l_total': _('Total:'),
                    'total': sum(rows.mapped('amount')),
                    'l_why': _('Why early:'), 'why': rec.note or '—',
                },
                partner_ids=rec._gm_partners().ids,
                subtype_xmlid='mail.mt_comment',
            )
        self.mapped('run_id')._refresh_register()
        return True

    def action_approve(self):
        """The GM approves every row this sub-batch has waiting on him.

        Deliberately does NOT finalise the month: approving one employee in
        the middle of it is not the month being finished.
        """
        self._check_gm()
        for rec in self:
            rec._check_month_open(_("Approving it"))
            rows = rec.sudo().entry_ids.filtered(
                lambda e: e.state == 'submitted')
            if not rows:
                raise UserError(_(
                    "%(name)s has nothing waiting for approval.",
                    name=rec.name))
            rows._wf_approve()
            rec.sudo().write({
                'approved_by': self.env.uid,
                'approved_date': fields.Datetime.now(),
            })
            rows._sync_containers()
            rec.sudo().message_post(
                body=Markup(
                    '<strong>%(title)s</strong><br/>'
                    '<b>%(l_by)s</b> %(user)s<br/>'
                    '<b>%(l_rows)s</b> %(rows)s · <b>%(l_total)s</b> '
                    '%(total).2f<br/><i>%(note)s</i>'
                ) % {
                    'title': _('✅ Approved'),
                    'l_by': _('By:'), 'user': self.env.user.name,
                    'l_rows': _('Entries:'), 'rows': len(rows),
                    'l_total': _('Total:'),
                    'total': sum(rows.mapped('amount')),
                    'note': _('These rows are now locked. The rest of the '
                              'month stays open.'),
                },
                partner_ids=rec._supervisor_partners().ids,
                subtype_xmlid='mail.mt_comment',
            )
        self.mapped('run_id')._sync_state()
        self.mapped('run_id')._refresh_register()
        return True

    def action_open_review(self):
        """The GM's review screen: approve or return some employees, per
        component; the rest stay waiting on him."""
        self.ensure_one()
        self._check_gm()
        return self.env['ksw.pay.gm.review.wizard']._open_for(
            self, mode='review')

    def action_open_reopen(self):
        """Send approved rows back to the supervisor, while unpaid."""
        self.ensure_one()
        self._check_gm()
        return self.env['ksw.pay.gm.review.wizard']._open_for(
            self, mode='reopen')

    def _review_entries(self, mode):
        """What the GM's review wizard lists for this sub-batch."""
        self.ensure_one()
        wanted = 'approved' if mode == 'reopen' else 'submitted'
        return self.sudo().entry_ids.filtered(lambda e: e.state == wanted)

    # ------------------------------------------------------------------
    # An imported component, returned by the GM
    # ------------------------------------------------------------------
    def action_reimport(self):
        """Fetch this sub-batch's drivers again for its days.

        Driver Trips cannot be corrected by hand, so when the GM returns
        one the correction is in BAS (or a driver's cost centre) and then
        a fresh import — for these drivers and these days only.
        """
        self.ensure_one()
        self._check_can_prepare(_("Importing again"))
        if not self._importer_batches():
            raise UserError(_(
                "%(name)s has no imported component — correct its rows in "
                "their batches.", name=self.name))
        skipped = self._import_trips(self.sudo().employee_ids)
        self._collect_entries()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Imported Again'),
                'message': '\n'.join(skipped) or _(
                    "%(name)s: trips imported again for its days.",
                    name=self.name),
                'type': 'warning' if skipped else 'success',
                'sticky': bool(skipped),
                'next': {'type': 'ir.actions.client', 'tag': 'reload'},
            },
        }

    def action_open_entries(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Entries — %(name)s', name=self.name),
            'res_model': 'ksw.pay.entry',
            'view_mode': 'list,form',
            'domain': [('x_sub_batch_id', '=', self.id)],
            'context': {'search_default_grp_employee': 1},
        }
