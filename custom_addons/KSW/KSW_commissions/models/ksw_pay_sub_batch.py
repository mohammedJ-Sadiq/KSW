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
    @api.depends('submission_id')
    def _compute_allowed_employees(self):
        Entry = self.env['ksw.pay.entry']
        for rec in self:
            rec.allowed_employee_ids = Entry.search([
                ('batch_id.submission_id', '=', rec.submission_id.id),
                ('state', '=', 'draft'),
            ]).employee_id if rec.submission_id else False

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
        res = super().write(vals) if vals else True
        if commands is not None:
            for rec in self:
                if not self.env.su:
                    rec._check_can_prepare(_("Changing its employees"))
                rec._write_employees(commands)
            self._collect_entries()
        return res

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
        known = self.env['ksw.pay.entry'].sudo().search([
            ('batch_id.submission_id', '=', self.submission_id.id),
        ]).employee_id
        stray = self.sudo().employee_ids - known
        if stray:
            raise UserError(_(
                "%(who)s has no entry in %(scope)s, so there is nothing to "
                "hand over for them.",
                who=', '.join(stray.mapped('name')),
                scope=self.submission_id.display_name))

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
            clash = self.sudo().search([
                ('id', '!=', rec.id),
                ('submission_id', '=', rec.submission_id.id),
                ('state', 'in', OPEN_STATES),
                ('employee_ids', 'in', rec.employee_ids.ids),
            ], limit=1)
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
        check_period_unlocked(self.env, self.period, what)
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

    def _collect_entries(self):
        """Tag every draft row of these employees in the department's month,
        and let go of the rows of anyone taken off the list."""
        Entry = self.env['ksw.pay.entry'].sudo()
        for rec in self.sudo():
            if rec.state not in ('draft', 'returned'):
                continue
            rows = Entry.search([
                ('batch_id.submission_id', '=', rec.submission_id.id),
                ('employee_id', 'in', rec.employee_ids.ids),
                ('state', '=', 'draft'),
                '|', ('x_sub_batch_id', '=', False),
                ('x_sub_batch_id', '=', rec.id),
            ])
            (rows - rec.entry_ids).write({'x_sub_batch_id': rec.id})
            rec.entry_ids.filtered(
                lambda e: e.state == 'draft'
                and e.employee_id not in rec.employee_ids
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
            rows.with_env(self.env)._check_not_held_for_submit(
                _("Submitting %s") % rec.name)
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
