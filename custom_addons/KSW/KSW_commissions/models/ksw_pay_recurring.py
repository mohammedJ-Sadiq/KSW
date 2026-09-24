"""KSW Recurring Pay Entry — SAP infotype 0014, the repeating half.

A lot of extra pay is the same every month: a mobile-phone allowance, a
project-management allowance. Recording it by hand twelve times a year is what
the old commission template existed for.

A recurring entry says "this employee gets this component, at this quantity,
between these dates". The supervisor pulls them into the month's batch with one
button, then adjusts anything that changed. It is deliberately a *pull*, not an
automatic posting — the supervisor stays responsible for what he submits.

A supervisor maintains his own team's standing instructions (19.0.3.5.0): they
are his to set up, and asking an Officer to type them was ceremony. Scope
follows the batch exactly — the same departments, the same cascading reporting
chain — through :meth:`ksw.pay.batch._allowed_departments` and
:meth:`~ksw.pay.batch._subordinate_employees`, so there is one definition of
"my people" in this module rather than two that can drift.
"""
from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .ksw_vacation_hold import hold_blocks, vacation_holds


class KswPayRecurring(models.Model):
    _name = 'ksw.pay.recurring'
    _description = 'KSW Recurring Pay Entry'
    _order = 'component_id, employee_id'

    # Never offer a picker wider than the user's authority. compute_sudo
    # because a supervisor has no model-level read on hr.employee — his own
    # searches are answered by hr.employee.public (CLAUDE.md gotcha #34) — and
    # assigning real employee records to a field as himself raises AccessError.
    # sudo() does not change the current user, so the list is still narrowed
    # to *his* reach.
    allowed_employee_ids = fields.Many2many(
        'hr.employee', compute='_compute_allowed_employees', compute_sudo=True,
        string='Employees I May Record For',
    )
    employee_id = fields.Many2one(
        'hr.employee', required=True, ondelete='cascade', index=True,
        domain="[('id', 'in', allowed_employee_ids)]",
    )
    component_id = fields.Many2one(
        'ksw.pay.component', required=True, ondelete='cascade', index=True,
    )
    option_id = fields.Many2one(
        'ksw.pay.option', string='Type', ondelete='cascade', index=True,
        domain="[('component_id', '=', component_id)]",
        help="Which of the component's choices repeats — a monthly lunch "
             "allowance rather than meals in general.",
    )
    has_options = fields.Boolean(
        related='component_id.has_options', readonly=True)
    needs_reason = fields.Boolean(
        related='component_id.needs_reason', readonly=True)
    quantity = fields.Float(default=1.0, digits=(16, 2))
    amount = fields.Monetary(
        help='For fixed-amount components, what to pay each month.',
    )
    currency_id = fields.Many2one(
        'res.currency', required=True,
        default=lambda s: s.env.company.currency_id,
    )
    reason = fields.Char(
        help="What this standing payment is for. On a catch-all component "
             "such as Other it is the only thing telling two of them apart, "
             "so it is required there and forms part of their identity.",
    )
    date_from = fields.Date(
        required=True, default=lambda s: fields.Date.context_today(s).replace(day=1),
    )
    date_to = fields.Date(help='Leave empty to run indefinitely.')
    active = fields.Boolean(default=True)

    department_id = fields.Many2one(
        related='employee_id.department_id', store=True, readonly=True,
    )

    # ------------------------------------------------------------------
    # Scope — one definition of "my people", shared with the batch
    # ------------------------------------------------------------------
    @api.model
    def _allowed_employees(self):
        """Whom the current user may set up a standing instruction for.

        The departments he manages or assists, plus his cascading reporting
        chain — two supervisors here manage people who sit in a different
        department from their own. Officers and above see everyone, because
        their whole job is the company-wide view.
        """
        Employee = self.env['hr.employee'].sudo()
        # Deliberately NOT `self.env.su`: this runs inside a compute_sudo
        # field, where su is true for everybody. Authority is the user's
        # group, which sudo() leaves alone.
        privileged = self.env.uid == SUPERUSER_ID or self.env.user.has_group(
            'KSW_commissions.group_commission_officer')
        if privileged:
            return Employee.search([])
        # …and for the same reason the authority question below has to be
        # asked with su dropped: `_allowed_departments` short-circuits on
        # `env.su` to *every* department, which inside the compute would hand
        # each supervisor all 400-odd employees — the exact picker this field
        # exists to narrow. The reads it does are sudo()'d internally, so it
        # loses nothing.
        Batch = self.env['ksw.pay.batch'].with_env(self.env(su=False))
        departments = Batch._allowed_departments()
        in_scope = Employee.search(
            [('department_id', 'in', departments.ids)]
        ) if departments else Employee.browse()
        return in_scope | Batch._subordinate_employees()

    @api.depends_context('uid')
    def _compute_allowed_employees(self):
        allowed = self._allowed_employees()
        for rec in self:
            rec.allowed_employee_ids = allowed

    def _check_employee_allowed(self):
        """Server-side twin of the ``employee_id`` domain.

        A narrowed picker is cosmetic — this is what stops an RPC call
        setting up a standing payment for somebody else's staff.
        """
        if self.env.su or self.env.user.has_group(
                'KSW_commissions.group_commission_officer'):
            return
        allowed = self._allowed_employees()
        for rec in self:
            if rec.employee_id not in allowed:
                raise UserError(_(
                    "%(employee)s does not report to you and is not in a "
                    "department you manage, so you cannot set up recurring "
                    "pay for them.",
                    employee=rec.employee_id.sudo().display_name))

    def _check_component_rights(self):
        """Server-side check that the user may record this component."""
        if self.env.su:
            return
        for rec in self:
            if not rec.component_id._check_may_enter():
                raise UserError(_(
                    "You are not allowed to record %(component)s.",
                    component=rec.component_id.name))

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._check_component_rights()
        records._check_employee_allowed()
        return records

    def write(self, vals):
        res = super().write(vals)
        if 'component_id' in vals:
            self._check_component_rights()
        if 'employee_id' in vals:
            self._check_employee_allowed()
        return res

    @staticmethod
    def _reason_key(reason):
        """Normalised reason, so spacing and case do not invent a difference."""
        return ' '.join((reason or '').split()).lower()

    # Python rather than SQL: a component without options leaves option_id
    # NULL, and Postgres indexes NULLS DISTINCT — a UNIQUE over the four
    # columns would wave every duplicate through. Same reasoning as
    # ksw.pay.batch._check_unique_scope.
    #
    # The component is not the identity of a standing instruction. A
    # catch-all like Other exists precisely so that one employee can carry
    # several unrelated monthly payments under it — a housing top-up and a
    # fuel card both start on the 1st and are told apart only by their
    # reason, which is why the component sets needs_reason. Keying on
    # (employee, component, option, date) alone refused the second one and
    # left the supervisor no way to record it. So the reason joins the key:
    # two differently-explained payments coexist, while typing the same one
    # twice — the accident this guard is for — is still refused.
    @api.constrains(
        'employee_id', 'component_id', 'option_id', 'date_from', 'reason')
    def _check_unique_employee_component(self):
        for rec in self:
            siblings = self.sudo().search([
                ('id', '!=', rec.id),
                ('employee_id', '=', rec.employee_id.id),
                ('component_id', '=', rec.component_id.id),
                ('option_id', '=', rec.option_id.id or False),
                ('date_from', '=', rec.date_from),
            ])
            key = rec._reason_key(rec.reason)
            duplicate = siblings.filtered(
                lambda s, k=key: s._reason_key(s.reason) == k)
            if duplicate:
                what = rec.option_id.name or rec.component_id.name
                if key:
                    raise ValidationError(_(
                        "%(employee)s already has a recurring %(what)s for "
                        "'%(reason)s' starting on that date. Give this one a "
                        "different reason if it is a separate payment.",
                        employee=rec.employee_id.display_name,
                        what=what, reason=rec.reason))
                raise ValidationError(_(
                    "%(employee)s already has a recurring %(what)s starting "
                    "on that date.",
                    employee=rec.employee_id.display_name, what=what))

    # Mirrors ksw.pay.entry._check_reason_required. Without it a standing
    # instruction could be saved with no reason and then break Add Recurring
    # for the whole department when the entry it creates hits that
    # constraint — refuse it here, where one supervisor can fix it.
    @api.constrains('reason', 'component_id')
    def _check_reason_required(self):
        for rec in self:
            if rec.component_id.needs_reason and not (rec.reason or '').strip():
                raise ValidationError(_(
                    "A recurring %(name)s needs a reason saying what it is "
                    "for.", name=rec.component_id.name))

    @api.constrains('option_id', 'component_id')
    def _check_option(self):
        for rec in self:
            if rec.component_id.has_options and not rec.option_id:
                raise ValidationError(_(
                    "A recurring %(name)s has to say which one it is: "
                    "%(options)s.",
                    name=rec.component_id.name,
                    options=', '.join(
                        rec.component_id.option_ids.mapped('name'))))
            if rec.option_id and rec.option_id.component_id != rec.component_id:
                raise ValidationError(_(
                    "'%(option)s' is not one of %(name)s's choices.",
                    option=rec.option_id.name, name=rec.component_id.name))

    @api.constrains('date_from', 'date_to')
    def _check_dates(self):
        for rec in self:
            if rec.date_to and rec.date_to < rec.date_from:
                raise ValidationError(_(
                    "The end date cannot be before the start date."))

    @api.depends('employee_id', 'component_id', 'option_id')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = '%s — %s' % (
                rec.employee_id.display_name or '',
                rec.option_id.name or rec.component_id.name or '')

    # ------------------------------------------------------------------
    @api.model
    def _apply_to_batch(self, batch):
        """Create the missing entries for ``batch``'s component and month.

        Idempotent: an employee who already has an entry in this batch is
        skipped, so the button can be pressed twice without duplicating.
        """
        period = batch.period
        domain = [
            ('component_id', '=', batch.component_id.id),
            ('date_from', '<=', period),
            '|', ('date_to', '=', False), ('date_to', '>=', period),
        ]
        if batch.department_id:
            domain.append(('employee_id.department_id', '=',
                           batch.department_id.id))
        # sudo() on the search, scoped by `in_scope` below. The record rule
        # matches a direct `parent_id` only, while `_allowed_employees` walks
        # the chain all the way down — without sudo() a second-level
        # subordinate's standing instruction would be silently skipped by the
        # button that exists to pull exactly those. `in_scope` is the
        # authoritative filter either way.
        recurring = self.sudo().search(domain)
        if not recurring:
            return self.env['ksw.pay.entry']

        # A component with options repeats per option, so "already there"
        # is the employee *and* the choice: pulling in the recurring meals
        # must not stop at the first one because his breakfast is typed.
        # The reason is in the key for the same reason it is in the
        # uniqueness key above — an employee may carry two standing Other
        # payments, and keying on the employee alone would pull the first
        # and silently drop the second.
        already = {(e.employee_id.id, e.option_id.id or False,
                    self._reason_key(e.reason))
                   for e in batch.entry_ids}
        # A recurring entry is company-wide configuration; this batch is not.
        # Pull in only the people it actually covers, or the entry guard
        # would reject the whole button for one out-of-scope row.
        in_scope = set(batch._allowed_employees().ids)
        # Whoever was on vacation this month is skipped rather than
        # refused: the entry guard raises, and a UserError here would cost
        # the whole department its standing instructions over one driver —
        # the same lesson the BAS importer records a few files over.
        entry_date = period if batch.component_id.needs_date else None
        holds = vacation_holds(self.env, recurring.employee_id, period)
        vals_list = []
        for rec in recurring:
            if (rec.employee_id.id, rec.option_id.id or False,
                    self._reason_key(rec.reason)) in already:
                continue
            if rec.employee_id.id not in in_scope:
                continue
            if hold_blocks(holds.get(rec.employee_id.id), entry_date):
                continue
            vals = {
                'batch_id': batch.id,
                'employee_id': rec.employee_id.id,
                'option_id': rec.option_id.id or False,
                'quantity': rec.quantity,
                'reason': rec.reason or False,
            }
            if batch.component_id.calculation == 'fixed':
                vals['amount'] = rec.amount
            if batch.component_id.needs_date:
                vals['date'] = period
            vals_list.append(vals)
        if not vals_list:
            return self.env['ksw.pay.entry']
        return self.env['ksw.pay.entry'].create(vals_list)
