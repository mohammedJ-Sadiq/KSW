"""KSW Pay Batch and Pay Entry — where extra pay is recorded.

A **batch** is one component, one scope, one month: *Maintenance · Overtime ·
August 2026*. It is the only screen a supervisor works in. An **entry** is one
line inside it — one occurrence of overtime, one employee's lunches for the
month, one allowance.

This is Oracle's Batch Element Entry and SAP's PA70 Fast Entry: a data-entry
convenience over a single flat fact table, not a business object per pay type.

Two deliberate choices, both explained at length in the design spec:

* **Entries are flat, grouped by employee in the view.** A per-employee
  sub-document would rebuild the very layering this redesign removes, and
  slow typing down for nothing — the reason and details belong to the
  occurrence, which a flat row already carries.
* **One component per batch**, so the columns can adapt: Overtime shows hours,
  location and reason; Meals shows a count; Import appears only where the
  component declares an importer. A component's *options* live inside the
  batch, not beside it — Breakfast, Lunch and Dinner are one Meals batch with
  a Type column, not three batches to open, submit and approve separately.
"""
import calendar
from collections import defaultdict
from datetime import datetime, time, timedelta

import pytz

from markupsafe import Markup, escape

from odoo import _, SUPERUSER_ID, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .ksw_commission_lock import check_period_unlocked, period_is_locked
from .ksw_pay_component import HOLIDAY_OCCASIONS
from .ksw_vacation_hold import (
    check_not_held, entry_blocked, hold_reason, month_bounds,
    pending_vacations, vacation_holds,
)

BATCH_STATES = [
    ('draft', 'Draft'),
    ('submitted', 'Submitted'),
    ('approved', 'Approved'),
]


class KswPayBatch(models.Model):
    _name = 'ksw.pay.batch'
    _description = 'KSW Pay Entry Batch'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'period desc, component_id, id desc'

    name = fields.Char(readonly=True, default='New', copy=False)
    component_id = fields.Many2one(
        'ksw.pay.component', required=True, ondelete='restrict',
        tracking=True, help='What kind of pay this batch records.',
    )
    period = fields.Date(
        required=True, tracking=True,
        default=lambda s: fields.Date.context_today(s).replace(day=1),
        help='First day of the month covered.',
    )
    department_id = fields.Many2one(
        'hr.department', ondelete='restrict', tracking=True,
        domain="[('id', 'in', allowed_department_ids)]",
    )
    # Legacy. No component has scope='site' since 19.0.4.3.0 — Driver
    # Trips, the only one that did, is recorded per department now — so
    # this is never set on a new batch and never shown (the field is
    # invisible unless scope == 'site', which no longer exists). Kept
    # because the batches already recorded against a site are history.
    site_id = fields.Many2one(
        'ksw.site', string='Work Site', ondelete='restrict', tracking=True,
    )
    # The department's handover to the GM. Created with the first batch of a
    # scope, and the thing that actually freezes this one: submitting a batch
    # only says "I have finished typing it", which is why it can still be
    # reopened — until the whole department has been handed over.
    submission_id = fields.Many2one(
        'ksw.pay.submission', string='Department Submission',
        ondelete='set null', index=True, readonly=True, copy=False,
    )
    submission_state = fields.Selection(
        related='submission_id.state', readonly=True, string='Handover',
    )
    run_id = fields.Many2one(
        related='submission_id.run_id', store=True, readonly=True,
    )

    # A supervisor may only record for a department he actually runs. The
    # picker is narrowed to those, and when there is exactly one it is
    # chosen for him and locked — never offer a choice that is not a choice.
    #
    # Relational + domain on purpose: a dynamic `selection=` cannot depend on
    # the record (the web client strips the context and caches the payload),
    # whereas a domain against a computed m2m is resolved per record.
    allowed_department_ids = fields.Many2many(
        'hr.department', compute='_compute_allowed_departments',
        string='Departments I May Record For',
    )
    department_locked = fields.Boolean(compute='_compute_allowed_departments')
    # Same idea one level down: the employee picker must not be wider than
    # the batch's own scope. Computed per record so it follows the
    # department or site actually chosen.
    # compute_sudo because a supervisor has no model-level read on
    # hr.employee — his own searches are answered by hr.employee.public
    # (CLAUDE.md gotcha #34), so assigning real employee records to a field
    # as himself raises AccessError. sudo() does not change the current
    # user, so the compute still narrows the list to *his* reach.
    allowed_employee_ids = fields.Many2many(
        'hr.employee', compute='_compute_allowed_employees', compute_sudo=True,
        string='Employees I May Record For',
    )
    state = fields.Selection(
        BATCH_STATES, default='draft', required=True, copy=False,
        tracking=True,
    )
    currency_id = fields.Many2one(
        'res.currency', required=True,
        default=lambda s: s.env.company.currency_id,
    )
    entry_ids = fields.One2many(
        'ksw.pay.entry', 'batch_id', string='Entries', copy=True,
    )
    note = fields.Text()

    # What the last import left out. Kept on the batch rather than
    # announced once in a toast: a run that silently drops people
    # needs a log on the document (same reasoning as
    # ksw.payslip.run.skip.line).
    skip_line_ids = fields.One2many(
        'ksw.pay.batch.skip.line', 'batch_id',
        string='Not Imported', copy=False,
    )
    skipped_count = fields.Integer(compute='_compute_skip_counts')
    review_count = fields.Integer(compute='_compute_skip_counts')
    # Rows paying an employee for something he already has in a sub-batch
    # (or the other way round). A warning on the form, never a block.
    x_overlap_count = fields.Integer(
        compute='_compute_overlap_count', string='Overlapping Rows')

    submitted_by = fields.Many2one('res.users', readonly=True, copy=False)
    submitted_date = fields.Datetime(readonly=True, copy=False)
    # This component was sent to the General Manager on its own, ahead of
    # the rest of the department. A plain Submit only says "I have finished
    # typing it"; this is what puts it in front of the GM
    # (entry._handed_over). Cleared only when the whole batch comes back
    # (action_return, action_reset_to_draft) — NOT when the GM returns some
    # of its rows, or the others would drop off his desk.
    handed_over_date = fields.Datetime(
        string='Sent to GM On', readonly=True, copy=False)
    return_reason = fields.Text(readonly=True, copy=False)

    # Mirrors of the component, so the view can adapt without a second read.
    calculation = fields.Selection(
        related='component_id.calculation', readonly=True,
    )
    qty_label = fields.Char(related='component_id.qty_label', readonly=True)
    qty_ref_label = fields.Char(
        related='component_id.qty_ref_label', readonly=True)
    scope = fields.Selection(related='component_id.scope', readonly=True)
    has_options = fields.Boolean(
        related='component_id.has_options', readonly=True)
    needs_date = fields.Boolean(
        related='component_id.needs_date', readonly=True)
    # True for a holiday bonus: its rows are dated from the Public Holidays
    # calendar, so the Date column shows the day and nobody types it.
    x_dated_by_calendar = fields.Boolean(
        compute='_compute_dated_by_calendar')
    needs_location = fields.Boolean(
        related='component_id.needs_location', readonly=True)
    importer = fields.Selection(
        related='component_id.importer', readonly=True)
    entries_import_only = fields.Boolean(
        related='component_id.entries_import_only', readonly=True)

    entry_count = fields.Integer(compute='_compute_totals', store=True)
    employee_count = fields.Integer(compute='_compute_totals', store=True)
    total_quantity = fields.Float(
        compute='_compute_totals', store=True, digits=(16, 2))
    total_amount = fields.Monetary(compute='_compute_totals', store=True)

    is_locked = fields.Boolean(compute='_compute_is_locked')
    x_can_reopen = fields.Boolean(compute='_compute_can_reopen')
    # Whether the Entries tab is read-only *for this user*. Deliberately no
    # model-level groups= on it: a readonly= expression in the view is
    # resolved against fields_get(), and a gated field is simply missing
    # there — the form would crash for everyone outside the group rather
    # than render read-only (Odoo 19 Pitfalls #31).
    x_entries_readonly = fields.Boolean(compute='_compute_entries_readonly')
    # Entries for somebody whose month was settled on his vacation request.
    # A banner rather than a silent filter: the rows are already there (the
    # guard below only stops new ones), and the supervisor is the one who
    # has to decide between deleting them and asking for a release.
    # Deliberately no model-level groups= — it is read by an invisible=
    # expression in the view (Odoo 19 Pitfalls #31).
    x_vacation_hold_warning = fields.Text(
        compute='_compute_vacation_hold_warning', compute_sudo=True,
        string='On Vacation',
    )

    # Deliberately NOT a SQL UNIQUE. A department-scoped batch leaves
    # site_id NULL (and vice versa), and Postgres indexes are NULLS
    # DISTINCT, so a table constraint would let two identical batches
    # through. Enforced in Python instead — see _check_unique_scope.

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends('entry_ids.amount', 'entry_ids.quantity',
                 'entry_ids.employee_id')
    def _compute_totals(self):
        for rec in self:
            rec.entry_count = len(rec.entry_ids)
            rec.employee_count = len(rec.entry_ids.mapped('employee_id'))
            rec.total_quantity = sum(rec.entry_ids.mapped('quantity'))
            # Whole riyals, by the register's own rule (each employee's
            # total per pay type, rounded half-up) — so the batches, the
            # department handovers and the month's earnings add up to the
            # same figure instead of three that differ by the halalas.
            rec.total_amount = sum(
                sum(by_component.values()) for by_component
                in self.env['ksw.pay.run']._rounded_component_totals(
                    rec.entry_ids).values())

    @api.depends('component_id.x_paid_day')
    def _compute_dated_by_calendar(self):
        occasions = dict(HOLIDAY_OCCASIONS)
        for rec in self:
            rec.x_dated_by_calendar = rec.component_id.x_paid_day in occasions

    @api.depends('state')
    def _compute_is_locked(self):
        for rec in self:
            rec.is_locked = rec.state != 'draft'

    @api.depends_context('uid')
    @api.depends('state', 'submission_id.state', 'period')
    def _compute_can_reopen(self):
        """May the current user pull this batch back to Draft?

        Yes while it is merely submitted and his department has not been
        handed over — correcting your own work before anyone has looked at
        it should never need a request. No once the handover is made: from
        there it is the GM's to return.
        """
        is_gm = self.env.user.has_group('KSW_commissions.group_commission_gm')
        locked = {}
        for rec in self:
            if rec.period not in locked:
                locked[rec.period] = period_is_locked(self.env, rec.period)
            if rec.state == 'draft' or locked[rec.period]:
                rec.x_can_reopen = False
                continue
            if rec.state == 'approved':
                # Money that has left is history, for the GM too.
                rec.x_can_reopen = is_gm and not rec.sudo().entry_ids.filtered(
                    lambda e: e.state == 'approved')._paid_entries()
                continue
            rec.x_can_reopen = is_gm or rec.submission_id.state != 'submitted'

    @api.depends_context('uid')
    @api.depends('component_id', 'component_id.entries_import_only')
    def _compute_entries_readonly(self):
        """An imported batch is reviewed, not typed.

        Driver Trips comes out of BAS whole — a weighted trip count and
        the allowance it earned. There is nothing in it a supervisor is in
        a position to correct: a wrong figure means a wrong cost centre or
        wrong data in BAS, and both are fixed there and re-imported, not
        typed over here. So the tab is read-only for him and there is no
        Add a line. Settings Administrator is exempt because somebody has
        to be able to intervene.

        ``depends_context('uid')`` is not optional — without it the ORM
        cache hands the first user's answer to everyone after him in the
        same request (Odoo 19 Pitfalls #14).
        """
        privileged = self.env.su or self.env.user.has_group(
            'base.group_system')
        for rec in self:
            rec.x_entries_readonly = (
                rec.entries_import_only and not privileged)

    @api.model
    def _allowed_departments(self, user=None):
        """Departments ``user`` may record pay for.

        A supervisor runs his own department and nobody else's. Two ways in,
        both of which the rest of this system already uses:

        * he is the department's manager (``hr.department.manager_id``);
        * he assists that manager — the two-key delegation from
          KSW_base_security, where ``x_assisted_manager_ids`` lists the
          managers a user may prepare work for but never approve for.

        Officers and above see everything, because their whole job is the
        company-wide view.
        """
        user = user or self.env.user
        Department = self.env['hr.department']
        if self.env.su or user.has_group(
                'KSW_commissions.group_commission_officer'):
            return Department.sudo().search([])

        manager_users = user | user.sudo().x_assisted_manager_ids
        employees = self.env['hr.employee'].sudo().search([
            ('user_id', 'in', manager_users.ids),
        ])
        if not employees:
            return Department
        return Department.sudo().search([('manager_id', 'in', employees.ids)])

    @api.depends_context('uid')
    def _compute_allowed_departments(self):
        allowed = self._allowed_departments()
        for rec in self:
            rec.allowed_department_ids = allowed
            # Exactly one option is not a choice — pick it and lock it.
            rec.department_locked = len(allowed) == 1

    @api.model
    def _subordinate_employees(self, user=None):
        """Everyone below ``user`` in the reporting chain — cascading.

        ``child_of`` walks ``hr.employee.parent_id`` all the way down, so a
        supervisor reaches his team leaders' people too, not just his direct
        reports. Assisted managers come along for the same reason they do
        everywhere else in this module: an assistant prepares the work his
        manager is answerable for.
        """
        user = user or self.env.user
        Employee = self.env['hr.employee'].sudo()
        managers = user | user.sudo().x_assisted_manager_ids
        roots = Employee.search([('user_id', 'in', managers.ids)])
        if not roots:
            return Employee.browse()
        return Employee.search([('id', 'child_of', roots.ids)])

    def _allowed_employees(self):
        """Whom this batch may pay.

        The batch's own scope decides the pool — the department it covers,
        or the work site, which is also exactly what the BAS driver import
        picks up. That pool is then widened by the user's own reporting
        chain, because two supervisors here manage people who sit in a
        different department from their own.
        """
        self.ensure_one()
        Employee = self.env['hr.employee'].sudo()
        # Deliberately NOT `self.env.su`: this runs inside a compute_sudo
        # field, where su is true for everybody. Authority is the user's
        # group, which sudo() leaves alone.
        privileged = self.env.uid == SUPERUSER_ID or self.env.user.has_group(
            'KSW_commissions.group_commission_officer')

        # Branch on the component's declared scope, not on whichever field
        # happens to hold a value — a batch can carry a stale department
        # from before its component was chosen.
        scope = self.component_id.scope
        if self.department_id and scope in ('department', False):
            in_scope = Employee.search(
                [('department_id', '=', self.department_id.id)])
        elif self.site_id and scope in ('site', False):
            in_scope = Employee.search([('x_site_id', '=', self.site_id.id)])
        elif privileged:
            return Employee.search([])
        else:
            # …and for the same reason the authority question has to be
            # asked with su dropped: `_allowed_departments` short-circuits
            # on `env.su` to *every* department, which inside this
            # compute_sudo field hands each supervisor all 400-odd
            # employees — the exact picker this method exists to narrow.
            # The trap was dormant while a batch always had a scope; it
            # goes off the moment one does not, which is every new batch
            # before its site is chosen (KSWCO, Sep 2026). The reads
            # inside are sudo()'d, so nothing is lost.
            batch = self.with_env(self.env(su=False))
            departments = batch._allowed_departments()
            in_scope = Employee.search(
                [('department_id', 'in', departments.ids)]
            ) if departments else Employee.browse()

        if privileged:
            return in_scope
        return in_scope | self._subordinate_employees()

    @api.depends_context('uid')
    @api.depends('department_id', 'site_id')
    def _compute_allowed_employees(self):
        # Only for the roles that actually type an entry. A General Manager
        # or an Accountant opens this form to read it, and filling a picker
        # he will never use is not free: reading an x2many back filters it
        # on `active` in the *reader's* environment
        # (Many2many.convert_to_record), so a single employee his role has
        # no hr.employee rule for raises AccessError for the whole form.
        # Same uid-not-su reasoning as _allowed_employees above.
        may_record = self.env.uid == SUPERUSER_ID or self.env.user.has_group(
            'KSW_commissions.group_commission_supervisor')
        for rec in self:
            rec.allowed_employee_ids = (
                rec._allowed_employees() if may_record
                else self.env['hr.employee'])

    @api.depends('period', 'entry_ids.employee_id', 'entry_ids.date')
    def _compute_vacation_hold_warning(self):
        for rec in self:
            rec.x_vacation_hold_warning = False
            # Not on a month that has already been paid. The money left the
            # bank; telling the reader it "will not be paid" would be a
            # lie, and there is nothing left to act on either way.
            if period_is_locked(self.env, rec.period):
                continue
            held = rec.entry_ids.filtered('x_vacation_hold')
            if not held:
                continue
            lines = []
            for employee in held.employee_id:
                entry = held.filtered(
                    lambda e, emp=employee: e.employee_id == emp)[:1]
                lines.append('\u2022 %s \u2014 %s' % (
                    employee.sudo().display_name, entry.x_vacation_hold))
            rec.x_vacation_hold_warning = _(
                "These people were on vacation in %(period)s. What they had "
                "earned was settled on the leave request itself, so paying "
                "it here pays it twice \u2014 remove the entries, or ask the "
                "General Manager to release the month.\n\n%(list)s",
                period=rec.period.strftime('%B %Y') if rec.period else '',
                list='\n'.join(lines),
            )

    @api.depends('skip_line_ids.outcome')
    def _compute_skip_counts(self):
        for rec in self:
            lines = rec.skip_line_ids
            rec.skipped_count = len(
                lines.filtered(lambda l: l.outcome == 'skipped'))
            rec.review_count = len(
                lines.filtered(lambda l: l.outcome == 'warning'))

    @api.depends('entry_ids.x_overlap_note')
    def _compute_overlap_count(self):
        for rec in self:
            rec.x_overlap_count = len(
                rec.entry_ids.filtered(lambda e: e.x_overlap_note))

    def action_clear_skip_log(self):
        """Drop the log once it has been dealt with."""
        self.ensure_one()
        self._check_editable_batch(_('Clearing the import log'))
        self.skip_line_ids.sudo().unlink()
        return True

    def _check_editable_batch(self, what):
        """The batch's own draft/lock guard, without an entry in hand."""
        if self.env.su:
            return
        check_period_unlocked(self.env, self.period, what)
        if self.state != 'draft':
            raise UserError(_(
                "%(name)s has been submitted. %(what)s is only possible "
                "while it is in Draft.", name=self.name, what=what))

    def _held_entries(self):
        """The entries this batch may not actually pay.

        Narrower than the banner above: an undated row in the month the
        employee came back is shown as a warning but not refused, because
        a monthly figure has no day to compare against.
        """
        self.ensure_one()
        # Only what is still the supervisor's to hand over. A row already
        # approved in a sub-batch is not his to delete; the register holds
        # it back and the vacation settlement pays it.
        entries = self.entry_ids.filtered(
            lambda e: e.employee_id and e.state == 'draft')
        if not entries or not self.period:
            return self.env['ksw.pay.entry']
        holds = vacation_holds(self.env, entries.employee_id, self.period)
        # A row the vacation payslip already paid stays on the batch as the
        # record of that payment (and cannot be deleted); it is left out of
        # the register, so it is no reason to refuse the handover.
        return entries.filtered(
            lambda e: not e.x_vacation_payslip_id
            and entry_blocked(holds.get(e.employee_id.id), e))

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        if 'department_id' in fields_list and not vals.get('department_id'):
            allowed = self._allowed_departments()
            if len(allowed) == 1:
                vals['department_id'] = allowed.id
        return vals

    @api.depends('component_id', 'department_id', 'site_id', 'period')
    def _compute_display_name(self):
        for rec in self:
            scope = rec.department_id.name or rec.site_id.name or _('Company')
            period = rec.period.strftime('%b %Y') if rec.period else ''
            rec.display_name = '%s · %s · %s' % (
                scope, rec.component_id.name or '', period)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    @api.constrains('component_id', 'department_id', 'site_id')
    def _check_scope(self):
        for rec in self:
            scope = rec.component_id.scope
            if scope == 'department' and not rec.department_id:
                raise ValidationError(_(
                    "'%(name)s' is recorded per department, so this batch "
                    "needs one.", name=rec.component_id.name))
            if scope == 'site' and not rec.site_id:
                raise ValidationError(_(
                    "'%(name)s' is recorded per work site, so this batch "
                    "needs one.", name=rec.component_id.name))

    @api.constrains('component_id', 'period', 'department_id', 'site_id')
    def _check_unique_scope(self):
        """One batch per component, scope and month.

        In Python because the equivalent SQL UNIQUE would never fire: the
        unused scope column is NULL and Postgres treats NULLs as distinct,
        so two identical department batches would both be accepted.
        """
        for rec in self:
            duplicate = self.sudo().search([
                ('id', '!=', rec.id),
                ('component_id', '=', rec.component_id.id),
                ('period', '=', rec.period),
                ('department_id', '=', rec.department_id.id or False),
                ('site_id', '=', rec.site_id.id or False),
            ], limit=1)
            if duplicate:
                raise ValidationError(_(
                    "%(existing)s already covers %(component)s for this "
                    "scope and month.",
                    existing=duplicate.name,
                    component=rec.component_id.name,
                ))

    @api.onchange('component_id')
    def _onchange_component_id(self):
        """Keep the scope fields matching the component — both ways.

        The empty-component guard is not cosmetic. Odoo runs the onchange
        methods once when a form is opened blank, before anything is chosen,
        so without it this cleared the department that ``default_get`` had
        just preselected — leaving a field that was empty *and* locked, and a
        required-field error the user had no way to answer.
        """
        for rec in self:
            if not rec.component_id:
                continue
            if rec.component_id.scope == 'department':
                if not rec.department_id:
                    # Restore the preselection when switching back to a
                    # department-scoped component.
                    allowed = rec._allowed_departments()
                    if len(allowed) == 1:
                        rec.department_id = allowed
            else:
                rec.department_id = False
            if rec.component_id.scope != 'site':
                rec.site_id = False

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def _clear_unused_scope(self, vals):
        """Drop the scope field this component does not use.

        The onchange does this in the UI, but ``default_get`` preselects the
        supervisor's department for *every* new batch — including a
        site-scoped one, which then silently carried a department it has no
        business having, and drew its employee list from it.
        """
        component = self.env['ksw.pay.component'].browse(
            vals.get('component_id'))
        if not component:
            return
        if component.scope != 'department':
            vals['department_id'] = False
        if component.scope != 'site':
            vals['site_id'] = False

    @api.model_create_multi
    def create(self, vals_list):
        Seq = self.env['ir.sequence']
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == 'New':
                vals['name'] = Seq.next_by_code('ksw.pay.batch') or 'New'
            if vals.get('period'):
                vals['period'] = fields.Date.to_date(
                    vals['period']).replace(day=1)
            self._clear_unused_scope(vals)
            check_period_unlocked(
                self.env, vals.get('period'), _("Creating a batch"))
        batches = super().create(vals_list)
        batches._check_component_rights()
        batches._check_department_rights()
        batches._ensure_submission()
        batches._check_department_open(_("Creating a batch"))
        return batches

    def _check_department_open(self, what):
        """A department handed over as a whole takes no new batch.

        Its batches are frozen, and a new one could never be handed over —
        the department is already in, or approved — so the rows typed into
        it would sit there unpaid with nothing to say why. Once the GM
        returns something, the department is open again.
        """
        if self.env.su:
            return
        for rec in self:
            if rec.submission_id.state in ('submitted', 'approved'):
                raise UserError(_(
                    "%(scope)s has been handed to the General Manager as a "
                    "whole. %(what)s is not possible until he returns it.",
                    scope=rec.submission_id.display_name, what=what))

    def write(self, vals):
        if vals.get('period'):
            vals['period'] = fields.Date.to_date(vals['period']).replace(day=1)
        if vals.get('component_id'):
            self._clear_unused_scope(vals)
        if not self.env.su:
            protected = set(vals) - {
                'state', 'note', 'submitted_by', 'submitted_date',
                'handed_over_date',
                'return_reason', 'message_follower_ids', 'message_ids',
                'activity_ids', 'message_main_attachment_id',
            }
            for rec in self:
                check_period_unlocked(
                    self.env, rec.period, _("Changing this batch"))
                if rec.state != 'draft' and protected:
                    raise UserError(_(
                        "Batch %(name)s has been submitted. Ask the General "
                        "Manager to return it before changing it.",
                        name=rec.name))
                # An open batch can still hold rows the GM has; those rows
                # are tied to this component, month and department.
                if protected & {'component_id', 'period', 'department_id',
                                'site_id', 'currency_id'} \
                        and rec.entry_ids.filtered(
                            lambda e: e.state != 'draft'):
                    raise UserError(_(
                        "%(name)s holds entries that have been handed to "
                        "the General Manager, so its component, month and "
                        "department can no longer change.", name=rec.name))
        res = super().write(vals)
        if 'state' in vals and not self.env.context.get('ksw_entry_sync'):
            # A blunt write of the batch's state (a data fix, an old
            # script) means the whole batch — the rows follow it, except a
            # row already paid, which nothing moves.
            self.entry_ids.filtered(
                lambda e: not e.x_vacation_payslip_id
                and e.state != vals['state']
            ).sudo().write({'state': vals['state']})
        if 'component_id' in vals:
            self._check_component_rights()
        if 'department_id' in vals:
            self._check_department_rights()
        if {'period', 'component_id'} & set(vals):
            # A holiday bonus moved to another month takes that month's
            # holiday, or is refused if the month has none.
            self.entry_ids.filtered(
                lambda e: e.state == 'draft' and not e.x_vacation_payslip_id
            )._apply_paid_day()
        if {'period', 'department_id', 'site_id'} & set(vals):
            self._ensure_submission()
            self._check_department_open(_("Moving a batch into it"))
        return res

    def _ensure_submission(self):
        """Attach every batch to the handover record for its scope.

        Creates the month and the submission on first use, so a supervisor
        never has to think about either: he opens a batch, and the month
        exists with his department already listed on it as outstanding.
        """
        Submission = self.env['ksw.pay.submission']
        for rec in self:
            if not rec.period:
                continue
            submission = Submission._for_scope(
                rec.period, department=rec.department_id, site=rec.site_id)
            if rec.submission_id != submission:
                rec.sudo().write({'submission_id': submission.id})

    def _sync_state_from_entries(self):
        """Make the batch read what its rows say.

        An open batch (draft) stays open whatever a sub-batch did to some of
        its rows — that is the point of splitting: the supervisor keeps
        typing while those rows wait on the GM. Once the batch has been
        submitted as a whole, it follows its rows: back to draft when the GM
        returns any of them, approved once all of them are.
        """
        for rec in self.sudo():
            if rec.state == 'draft':
                continue
            states = set(rec.entry_ids.mapped('state'))
            if not states or 'draft' in states:
                target = 'draft'
            elif 'submitted' in states:
                target = 'submitted'
            else:
                target = 'approved'
            if target != rec.state:
                vals = {'state': target}
                if target == 'draft':
                    vals.update(submitted_by=False, submitted_date=False)
                rec.with_context(ksw_entry_sync=True).write(vals)
        return True

    def unlink(self):
        # The entries go by ondelete='cascade', which never calls their own
        # unlink(), so their paid-on-vacation lock has to be asked here.
        self.entry_ids._check_not_settled(_("Deleting its batch"))
        # Not exempting env.su, on purpose: a batch holding rows the GM has
        # or has approved is part of an approval, not a draft, and nothing
        # — no cleanup, no administrator — should make it disappear.
        handed = self.entry_ids.filtered(lambda e: e.state != 'draft')
        if handed:
            raise UserError(_(
                "%(name)s cannot be deleted: %(count)s of its entries have "
                "been handed to or approved by the General Manager.",
                name=handed[0].batch_id.name, count=len(handed)))
        for rec in self:
            check_period_unlocked(
                self.env, rec.period, _("Deleting this batch"))
            if rec.state != 'draft' and not self.env.su:
                raise UserError(_(
                    "Only a draft batch can be deleted. %(name)s is %(state)s.",
                    name=rec.name, state=rec.state))
        return super().unlink()

    def _check_no_held_entries(self):
        """Refuse to hand over a month that pays a settled vacation twice."""
        if self.env.su:
            return
        self.ensure_one()
        held = self._held_entries()
        if not held:
            return
        raise UserError(_(
            "%(name)s cannot be submitted: %(count)s entr%(plural)s "
            "belong%(verb)s to somebody whose %(period)s was already "
            "settled on his vacation request.\n\n%(who)s\n\n"
            "Delete those entries, or ask the General Manager to release "
            "the month (Commissions \u2192 Vacation Releases).",
            name=self.name,
            count=len(held),
            plural=_('y') if len(held) == 1 else _('ies'),
            verb=_('s') if len(held) == 1 else '',
            period=self.period.strftime('%B %Y') if self.period else '',
            who=', '.join(held.employee_id.sudo().mapped('display_name')),
        ))

    def _check_component_rights(self):
        """Server-side check that the user may record this component.

        The picker is filtered too, but a filtered picker is cosmetic — this
        is what stops an RPC call.
        """
        if self.env.su:
            return
        for rec in self:
            if not rec.component_id._check_may_enter():
                raise UserError(_(
                    "You are not allowed to record %(component)s.",
                    component=rec.component_id.name))

    def _check_department_rights(self):
        """Server-side twin of the ``department_id`` domain."""
        if self.env.su:
            return
        allowed = self._allowed_departments()
        for rec in self:
            if not rec.department_id:
                continue
            if rec.department_id not in allowed:
                if not allowed:
                    raise UserError(_(
                        "You are not set as the manager of any department, so "
                        "there is nothing you can record pay for. Ask HR to "
                        "set you as the manager of your department, or to "
                        "make you an assistant to its manager."))
                raise UserError(_(
                    "You may only record pay for %(allowed)s.",
                    allowed=', '.join(allowed.mapped('name'))))

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def action_submit(self):
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_(
                    "Only a draft batch can be submitted."))
            if not rec.entry_ids:
                raise UserError(_(
                    "There is nothing to submit — %(name)s has no entries.",
                    name=rec.name))
            rec._check_component_rights()
            check_period_unlocked(self.env, rec.period, _("Submitting"))
            rec._check_no_held_entries()
            rec._ensure_submission()
            # Rows a sub-batch already handed over stay where they are;
            # everything still in draft goes with the batch.
            rec.entry_ids._wf_submit()
            rec.with_context(ksw_entry_sync=True).write({
                'state': 'submitted',
                'submitted_by': self.env.uid,
                'submitted_date': fields.Datetime.now(),
                'return_reason': False,
            })
            # Nothing left pending (every row came in a sub-batch the GM
            # already approved) reads as approved, not as waiting.
            rec._sync_state_from_entries()
            rec.entry_ids.x_sub_batch_id._sync_state_from_entries()
            rec.sudo().message_post(
                body=Markup(
                    '<strong>Submitted</strong><br/>'
                    '<b>Entries:</b> %(count)s across %(emp)s employee(s)'
                    '<br/><b>Total:</b> %(total).2f'
                ) % {'count': rec.entry_count, 'emp': rec.employee_count,
                     'total': rec.total_amount or 0.0},
                subtype_xmlid='mail.mt_note',
            )
        self.mapped('submission_id.run_id')._refresh_register()
        return True

    def action_return(self, reason=None):
        """Send a submitted batch back to its supervisor."""
        for rec in self:
            if rec.state != 'submitted':
                raise UserError(_(
                    "Only a submitted batch can be returned."))
            partners = rec.submitted_by.partner_id
            rec.entry_ids._wf_return(reason)
            rec.with_context(ksw_entry_sync=True).write({
                'state': 'draft',
                'return_reason': reason or False,
                'submitted_by': False,
                'submitted_date': False,
                'handed_over_date': False,
            })
            rec.entry_ids.x_sub_batch_id._sync_state_from_entries()
            rec.sudo().message_post(
                body=Markup(
                    '<strong>Returned for correction</strong><br/>'
                    '<b>By:</b> %(user)s<br/><b>Reason:</b> %(reason)s'
                ) % {'user': self.env.user.name, 'reason': reason or '—'},
                partner_ids=partners.ids if partners else [],
                subtype_xmlid='mail.mt_comment',
            )
            # A department cannot stay handed over while one of its batches
            # is back on the supervisor's desk.
            if rec.submission_id.state == 'submitted':
                rec.submission_id.sudo().write({
                    'state': 'returned', 'returned_by': self.env.uid,
                    'return_reason': reason or rec.submission_id.return_reason,
                    'submitted_by': False, 'submitted_date': False,
                })
        runs = self.mapped('submission_id.run_id')
        runs._sync_state()
        runs._refresh_register()
        return True

    def action_reset_to_draft(self):
        """Pull a batch back to Draft to correct it.

        The supervisor's own to do, right up until he hands the department
        over: before that nobody has looked at it, so needing to ask would be
        ceremony for its own sake. After it, the GM returns it — which is the
        same action with a reason attached.
        """
        is_gm = self.env.user.has_group('KSW_commissions.group_commission_gm')
        for rec in self:
            check_period_unlocked(
                self.env, rec.period, _("Reopening this batch"))
            # Not GM-exempt, unlike the period lock above: a finalised month
            # has its register built from these rows, so reopening one batch
            # inside it would leave the bank file paying rows that read as
            # draft. The month is reopened first (ksw.pay.run.action_reopen).
            if not self.env.su and period_is_locked(self.env, rec.period):
                raise UserError(_(
                    "%(month)s has been finalised. Reopen the month first — "
                    "a batch cannot be reopened inside a finalised month.",
                    month=rec.period.strftime('%B %Y')))
            # Rows a sub-batch handed over are the sub-batch's to reopen;
            # a row that has been paid is nobody's.
            rows = (rec.entry_ids - rec.entry_ids._sub_batch_controlled())
            if self.env.su or is_gm:
                approved = rows.filtered(lambda e: e.state == 'approved')
                if not self.env.su:
                    approved._check_reopenable()
                rows.filtered(
                    lambda e: e.state != 'draft' and not e.x_vacation_payslip_id
                ).sudo().write({'state': 'draft'})
                rec.with_context(ksw_entry_sync=True).write({
                    'state': 'draft', 'submitted_by': False,
                    'submitted_date': False, 'handed_over_date': False})
                rec.submission_id._sync_state_from_entries()
                continue
            if rec.state == 'approved':
                raise UserError(_(
                    "%(name)s has been approved. Only the General Manager "
                    "can reopen it.", name=rec.name))
            if rec.submission_id.state == 'submitted':
                raise UserError(_(
                    "%(scope)s has already been submitted to the General "
                    "Manager, so its batches are frozen. Ask him to return "
                    "it — or, if he has not looked yet, take the submission "
                    "back from the Monthly Pay Run.",
                    scope=rec.submission_id.display_name))
            # Taking back a component sent on its own is the batch-sized
            # twin of the department's Take Back: allowed until the GM acts.
            rows._wf_return()
            rec.with_context(ksw_entry_sync=True).write({
                'state': 'draft', 'submitted_by': False,
                'submitted_date': False, 'handed_over_date': False})
        self.mapped('submission_id.run_id')._refresh_register()
        return True

    # ------------------------------------------------------------------
    # Entry helpers
    # ------------------------------------------------------------------
    def action_add_recurring(self):
        """Materialise this component's recurring entries into the batch.

        Anyone among them with a vacation still being approved is the
        supervisor's call, not ours: the prompt lists them all at once.
        """
        self.ensure_one()
        Recurring = self.env['ksw.pay.recurring']
        due = Recurring._due_for_batch(self)
        if pending_vacations(self.env, due.employee_id, self.period):
            return self.env['ksw.pay.recurring.vacation.wizard'] \
                ._open_for_batch(self)
        return self._recurring_added(Recurring._apply_to_batch(self))

    def _recurring_added(self, created):
        return self._notify(_(
            "%(count)s recurring entr%(plural)s added.",
            count=len(created), plural=_('y') if len(created) == 1 else _('ies'),
        ))

    def action_hand_over(self):
        """Send these components to the General Manager on their own.

        The supervisor's choice of *which* pay components go now; the rest
        of his department stays open for him. It never hands the department
        over by itself — not even the last component: saying "my month is
        finished" is the explicit "Every component" choice, because the
        department's approval is what can finalise and lock the month.
        """
        self.mapped('submission_id')._check_mine()
        batches = self.filtered(lambda b: b._has_something_to_send())
        if not batches:
            raise UserError(_("Pick at least one component to send."))
        batches.filtered(lambda b: b.state == 'draft').action_submit()
        now = fields.Datetime.now()
        for rec in batches:
            if rec.state == 'submitted':
                rec.with_context(ksw_entry_sync=True).write(
                    {'handed_over_date': now})
        for submission in batches.submission_id:
            sent = batches.filtered(lambda b, s=submission: b.submission_id == s)
            body = Markup(
                '<strong>%(title)s</strong><br/>'
                '<b>%(l_by)s</b> %(user)s<ul>'
            ) % {'title': _('📤 Components sent for approval'),
                 'l_by': _('By:'), 'user': self.env.user.name}
            for b in sent:
                body += Markup('<li>%(name)s — %(total).2f</li>') % {
                    'name': b.component_id.name,
                    'total': b.total_amount or 0.0}
            body += Markup('</ul>')
            submission.sudo().message_post(
                body=body, partner_ids=submission._gm_partners().ids,
                subtype_xmlid='mail.mt_comment')
        runs = batches.mapped('submission_id.run_id')
        runs._sync_state()
        runs._refresh_register()
        return True

    def _has_something_to_send(self):
        """Does this component carry anything the GM does not have yet?

        Draft rows, or rows the supervisor already submitted batch by batch
        ("finished typing") that are not in front of the GM. A batch whose
        rows all went in a sub-batch has nothing — sending it would only
        close it (PB016633, Sep 2026).
        """
        self.ensure_one()
        if self.state not in ('draft', 'submitted'):
            return False
        rows = self.sudo().entry_ids
        return bool(rows.filtered(lambda e: e.state == 'draft')
                    or (rows.filtered(lambda e: e.state == 'submitted')
                        - rows._handed_over()))

    def action_send_early(self):
        """Send Early…: some employees of this component, for some days,
        to the General Manager now — one dialog, nothing else to press."""
        self.ensure_one()
        self._check_editable_batch(_("Sending employees early"))
        self._ensure_submission()
        self.submission_id._check_mine()
        wizard = self.env['ksw.pay.send.early.wizard'].create({
            'batch_id': self.id,
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _('Send Early — %(name)s', name=self.component_id.name),
            'res_model': wizard._name,
            'res_id': wizard.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_new_sub_batch(self):
        """The old "Sub-Batch" button's name. A browser tab left open across
        the upgrade keeps the cached form and still calls it — open the new
        dialog rather than an error."""
        return self.action_send_early()

    def action_import(self):
        """Run the importer the component declares."""
        self.ensure_one()
        if not self.component_id.importer:
            raise UserError(_(
                "%(name)s has no import source configured.",
                name=self.component_id.name))
        method = '_import_%s' % self.component_id.importer
        if not hasattr(self, method):
            raise UserError(_(
                "The import source '%(src)s' is not available.",
                src=self.component_id.importer))
        # Here rather than inside each importer: this is the one door they
        # all come through, so a future one is import-only-safe without
        # having to know the flag exists. Not sudo() on purpose — the
        # entries it writes must still face the period lock, the
        # draft-state check and the employee scope.
        return getattr(self.with_context(ksw_pay_importing=True), method)()

    def _notify(self, message, title=None):
        """Toast the outcome, then refresh the form.

        Every caller has just written entries, and returning an action
        *replaces* the reload the web client does for a button that returns
        nothing — so without the chained ``soft_reload`` the new lines sit in
        the database while the Entries tab still shows the old ones until the
        user reloads the page by hand. ``display_notification`` returns
        ``params['next']`` to the action service (see ``client_actions.js``),
        and ``soft_reload`` restores the current controller without a full
        browser reload.
        """
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': title or _('Pay Entries'),
                'message': message,
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }


class KswPayEntry(models.Model):
    """One thing an employee is being paid extra for."""
    _name = 'ksw.pay.entry'
    _description = 'KSW Pay Entry'
    _order = 'batch_id, employee_id, date, id'

    batch_id = fields.Many2one(
        'ksw.pay.batch', required=True, ondelete='cascade', index=True,
    )
    component_id = fields.Many2one(
        related='batch_id.component_id', store=True, readonly=True, index=True,
    )
    period = fields.Date(
        related='batch_id.period', store=True, readonly=True, index=True,
    )
    department_id = fields.Many2one(
        related='batch_id.department_id', store=True, readonly=True,
    )
    site_id = fields.Many2one(
        related='batch_id.site_id', store=True, readonly=True,
    )
    # The entry's own approval state — not the batch's. It used to be a
    # stored related of batch_id.state, which made the batch the smallest
    # thing a GM could approve or return. A supervisor handing over one
    # employee before his vacation, or a GM refusing one employee's
    # overtime and approving the rest, both need the decision to sit on the
    # row. The batch and the department submission are summaries of it
    # (_sync_state_from_entries), so a batch stays open while some of its
    # rows are locked.
    state = fields.Selection(
        BATCH_STATES, default='draft', required=True, readonly=True,
        copy=False, index=True, string='Status')
    # The sub-batch this row was handed over in, if it went early. Kept on
    # the row after approval as the record of how it was approved.
    x_sub_batch_id = fields.Many2one(
        'ksw.pay.sub.batch', string='Sub-Batch', readonly=True, copy=False,
        index=True, ondelete='set null')
    x_return_reason = fields.Text(
        string='Returned Because', readonly=True, copy=False)
    currency_id = fields.Many2one(
        related='batch_id.currency_id', readonly=True,
    )

    # No x_is_attendance_sheet filter: biometric employees earn overtime too
    # (Maintenance is 18/30 non-biometric, Workshop 7/14). The picker is
    # narrowed instead to the batch's own scope and the supervisor's
    # reporting chain — never offer a list wider than his authority.
    allowed_employee_ids = fields.Many2many(
        related='batch_id.allowed_employee_ids', readonly=True,
    )
    employee_id = fields.Many2one(
        'hr.employee', required=True, ondelete='restrict', index=True,
        domain="[('id', 'in', allowed_employee_ids)]",
    )
    # The component's own choices — the Meals batch's Breakfast / Lunch /
    # Dinner. Relational with a domain rather than a Selection: the options
    # vary per record, and a dynamic `selection=` cannot (the web client
    # strips the context and caches the payload).
    option_id = fields.Many2one(
        'ksw.pay.option', string='Type', ondelete='restrict', index=True,
        domain="[('component_id', '=', component_id)]",
        help='Which of this component\'s choices this row is — the rate '
             'comes from it.',
    )
    has_options = fields.Boolean(
        related='component_id.has_options', readonly=True)
    date = fields.Date(
        help='The day this occurred. Used when the component is recorded per '
             'occurrence; must fall inside the batch period.',
    )
    quantity = fields.Float(default=0.0, digits=(16, 2))
    quantity_ref = fields.Float(
        string='Reference Quantity', digits=(16, 2),
        help='A second figure recorded for justification but not used in the '
             'calculation — for driver trips, the raw trip count behind the '
             'weighted one.',
    )
    threshold_qty = fields.Float(
        string='Free Allowance', digits=(16, 2),
        help='Tiered components only: the quantity earned before payment '
             'starts — a driver\'s required trips for the days he worked.',
    )
    # Every work site except the one hidden record that holds the driver
    # trip settings — that one is a calculation, not a place, and must
    # never turn up in a picker.
    location_id = fields.Many2one(
        'ksw.site', string='Location',
        domain="[('site_type', '=', 'location')]",
        help='Where this occurred.',
    )
    reason = fields.Char()
    details = fields.Text(string='Further Details')

    rate = fields.Float(
        compute='_compute_amount', store=True, digits=(16, 4), readonly=True,
        help='Resolved from the component. Informational — the amount is not '
             'computed from this rounded figure.',
    )
    amount_computed = fields.Monetary(
        compute='_compute_amount', store=True, readonly=True,
    )
    amount_override = fields.Monetary(
        help='Pay something other than the computed amount. Unlike a plain '
             'writable compute this survives a later edit to the quantity.',
    )
    amount = fields.Monetary(compute='_compute_amount', store=True)
    is_overridden = fields.Boolean(compute='_compute_amount', store=True)

    # Why this row is a problem, in one line, or False when it is not.
    # Not stored: it is an answer about a leave that can change after the
    # entry was typed (a return gets confirmed, a month gets released),
    # and a stale stored copy is worse than no copy.
    x_vacation_hold = fields.Char(
        compute='_compute_vacation_hold', compute_sudo=True,
        string='On Vacation',
    )
    # The vacation payslip that paid this entry. Set when that payslip is
    # confirmed, cleared if it is cancelled (hr.payslip.write in this
    # module); the monthly run leaves a stamped entry out of its register.
    # Never shown directly: a supervisor has no read on hr.payslip, so the
    # list reads x_vacation_settlement instead.
    x_vacation_payslip_id = fields.Many2one(
        'hr.payslip', string='Paid on Vacation Payslip', readonly=True,
        copy=False, index=True, ondelete='set null',
    )
    x_vacation_settlement = fields.Char(
        compute='_compute_vacation_settlement', compute_sudo=True,
        string='Paid on Vacation',
    )

    # The days an imported figure covers. A month's driver trips can be
    # paid in pieces — a sub-batch for 16–30 Sep, the batch's own import
    # for the days no sub-batch took — and each piece has to say which
    # days it is, or the next import cannot tell what is still unpaid.
    # Blank on typed rows and on rows imported before windows existed;
    # those cover the whole month (`_span`).
    x_window_from = fields.Date(
        string='Days From', readonly=True, copy=False,
        help='First day the imported figure covers.')
    x_window_to = fields.Date(
        string='Days To', readonly=True, copy=False,
        help='Last day the imported figure covers.')
    # The same employee paid for the same thing twice: once in a sub-batch
    # and again in the batch, or in another sub-batch. A warning, never a
    # block — the supervisor may mean it (two separate overtime sessions on
    # one day), and he is the one who knows. Not stored: it is an answer
    # about the other rows, which change without touching this one.
    x_overlap_note = fields.Char(
        compute='_compute_overlap_note', compute_sudo=True,
        string='Also In',
        help='This employee has another row for the same thing — the same '
             'day, overlapping days, or (for a monthly allowance) the same '
             'month — in a different sub-batch or in the batch itself. '
             'Check that it is not being paid twice.')

    # `date` and `period` are in here because the rate an employee has of
    # their own (ksw.pay.employee.rate) is dated: moving an occurrence into
    # another month can move it across a rate change. That model has no
    # relation to traverse back from, so it marks affected entries for
    # recompute itself — see its _recompute_affected_entries.
    #
    # The component's own pricing (calculation, rate, divisor, factor, tiers,
    # option rates) is deliberately NOT a dependency. As one, it restated
    # every entry ever recorded: switching National Day Bonus from Fixed to
    # Quantity × rate on KSWCO (6 Oct 2026) re-ran 18 entries of an
    # *approved* September batch as 0 × 100 and wiped 1,500.00 SAR. Those
    # models mark the open entries themselves — see _mark_open_for_repricing.
    @api.depends('employee_id', 'quantity', 'threshold_qty', 'amount_override',
                 'component_id', 'site_id', 'option_id', 'date', 'period',
                 'x_window_from', 'x_window_to')
    def _compute_amount(self):
        for rec in self:
            component = rec.component_id
            if component:
                rate, amount = component._resolve(
                    rec.employee_id,
                    quantity=rec.quantity,
                    site=rec.site_id or rec.location_id,
                    threshold=rec.threshold_qty,
                    option=rec.option_id,
                    date=rec.date or rec.period,
                    band_scale=rec._band_scale(),
                )
            else:
                rate, amount = 0.0, 0.0
            rec.rate = rate
            rec.amount_computed = amount
            rec.is_overridden = bool(rec.amount_override)
            if rec.amount_override:
                rec.amount = rec.amount_override
            elif component and component.calculation == 'fixed':
                # Nothing to derive: keep whatever was typed.
                rec.amount = rec.amount or 0.0
            else:
                rec.amount = amount

    _PRICE_FIELDS = ('rate', 'amount_computed', 'amount', 'is_overridden')

    @api.model
    def _mark_open_for_repricing(self, components=None, options=None):
        """Re-price the entries a pricing change may still reach.

        Only **draft** entries in an **unlocked** month — the rule
        ksw.pay.employee.rate already follows: what was submitted, approved
        or paid keeps the figure it was signed off at. Called *after* the
        write, or the flush inside it recomputes from the old values and
        consumes the mark.
        """
        domain = [('state', '=', 'draft')]
        if options:
            domain.append(('option_id', 'in', options.ids))
        elif components:
            domain.append(('component_id', 'in', components.ids))
        else:
            return self.browse()
        entries = self.sudo().search(domain).filtered(
            lambda e: not period_is_locked(self.env, e.period))
        for name in self._PRICE_FIELDS:
            self.env.add_to_compute(self._fields[name], entries)
        return entries

    @api.depends('employee_id', 'component_id', 'option_id', 'date')
    def _compute_display_name(self):
        for rec in self:
            what = rec.option_id.name or rec.component_id.name or ''
            rec.display_name = '%s — %s' % (
                rec.employee_id.display_name or '', what)

    # ------------------------------------------------------------------
    # "Why is it this much?"
    # ------------------------------------------------------------------
    explanation = fields.Html(
        compute='_compute_explanation', sanitize=False,
        string='How this amount was worked out',
    )

    @api.depends('amount', 'quantity', 'quantity_ref', 'threshold_qty',
                 'rate', 'amount_override', 'component_id', 'option_id',
                 'employee_id', 'date', 'period',
                 'x_window_from', 'x_window_to')
    def _compute_explanation(self):
        """Render the derivation as a table.

        A figure a supervisor cannot justify is a figure he cannot defend at
        review. The old driver-commission line showed the trips, the weighted
        trips and every tier; collapsing that to a single amount lost the
        justification, so every component now explains itself the same way.
        """
        for rec in self:
            rec.explanation = rec._build_explanation()

    def _build_explanation(self):
        self.ensure_one()
        component = self.component_id
        if not component:
            return False

        rows, notes = component._resolve_detail(
            self.employee_id,
            quantity=self.quantity,
            site=self.site_id or self.location_id,
            threshold=self.threshold_qty,
            option=self.option_id,
            date=self.date or self.period,
            band_scale=self._band_scale(),
        )

        if self.x_window_from and self.x_window_to \
                and self._band_scale() != 1.0:
            notes.insert(0, _(
                'Covers %(start)s to %(end)s only (%(days)d of %(month)d '
                'days), so the tier bands are cut to %(pct).0f%% of a '
                'full month.',
                start=self.x_window_from, end=self.x_window_to,
                days=(self.x_window_to - self.x_window_from).days + 1,
                month=calendar.monthrange(
                    self.period.year, self.period.month)[1],
                pct=self._band_scale() * 100))
        if self.quantity_ref and component.qty_ref_label:
            notes.insert(0, '%s: %s' % (
                component.qty_ref_label,
                self.env['ir.qweb.field.float'].value_to_html(
                    self.quantity_ref, {'precision': 2}),
            ))

        # Every value is escaped: `details` is free text a supervisor types,
        # and this field is sanitize=False, so an unescaped value here runs
        # as script in the browser of whoever opens the entry (GM, accounts).
        currency = escape(self.currency_id.symbol or '')
        html = [Markup('<table class="table table-sm o_main_table">'
                       '<thead><tr><th>%s</th>'
                       '<th class="text-end">%s</th>'
                       '<th class="text-end">%s</th>'
                       '<th class="text-end">%s</th>'
                       '</tr></thead><tbody>') % (
            _('Step'), component.qty_label or _('Quantity'),
            _('Rate'), _('Amount'))]
        for row in rows:
            html.append(Markup(
                '<tr><td>%s</td>'
                '<td class="text-end">%s</td>'
                '<td class="text-end">%s</td>'
                '<td class="text-end">%s</td></tr>') % (
                    row['label'],
                    ('%.2f' % row['quantity']) if row.get('quantity') is not None else '',
                    ('%.4f' % row['rate']) if row.get('rate') is not None else '',
                    ('%.2f' % row['amount']) if row.get('amount') is not None else '',
                ))
        html.append(Markup(
            '<tr class="fw-bold border-top">'
            '<td>%s</td><td></td><td></td>'
            '<td class="text-end">%s %s</td></tr>') % (
                _('Total'), '%.2f' % (self.amount or 0.0), currency))
        html.append(Markup('</tbody></table>'))

        if self.is_overridden:
            notes.append(_(
                'The computed amount was overridden by hand '
                '(computed: %(computed).2f).', computed=self.amount_computed))
        if self.details:
            notes.append(self.details)
        if notes:
            html.append(Markup('<ul class="mb-0">'))
            html.extend(Markup('<li>%s</li>') % note for note in notes)
            html.append(Markup('</ul>'))
        return Markup('').join(html)

    def action_explain(self):
        """Open the derivation for this line."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('How this amount was worked out'),
            'res_model': 'ksw.pay.entry',
            'res_id': self.id,
            'view_mode': 'form',
            'view_id': self.env.ref(
                'KSW_commissions.view_ksw_pay_entry_explain_form').id,
            'target': 'new',
        }

    # ------------------------------------------------------------------
    # The day a holiday bonus is paid for
    # ------------------------------------------------------------------
    @api.model
    def _paid_day_date(self, occasion, period):
        """The day ``occasion`` falls on in ``period``'s month, or False.

        Read from Time Off > Public Holidays: the company-wide holiday HR
        tagged with this occasion. An Eid spans several days; the LAST one
        inside the month is returned, so somebody back from vacation for
        any part of it is paid — the hold refuses only a day before his
        return, and the last day is the most generous honest answer.
        """
        month_start, month_end = month_bounds(period)
        if not month_start:
            return False
        tz = pytz.timezone(
            self.env.company.resource_calendar_id.tz or 'Asia/Riyadh')
        holidays = self.env['resource.calendar.leaves'].sudo().search([
            ('resource_id', '=', False),
            ('x_pay_occasion', '=', occasion),
            ('company_id', 'in', [False, self.env.company.id]),
            # One day of slack each way for the timezone; the exact
            # local dates are compared below.
            ('date_from', '<', datetime.combine(
                month_end + timedelta(days=2), time.min)),
            ('date_to', '>', datetime.combine(
                month_start - timedelta(days=1), time.min)),
        ])
        days = []
        for holiday in holidays:
            first = pytz.utc.localize(holiday.date_from).astimezone(tz).date()
            last = pytz.utc.localize(holiday.date_to).astimezone(tz).date()
            last = min(last, month_end)
            if max(first, month_start) <= last:
                days.append(last)
        return max(days) if days else False

    def _apply_paid_day(self, strict=True):
        """Date every row of a holiday bonus with its holiday.

        The supervisor never types it: the day is already known, and a
        typed one could only be wrong. With the row dated, the vacation
        hold judges it like any other dated row — payable when he was back
        by then, refused when he was still away — instead of flagging every
        returnee for the whole month.

        :param strict: raise when the calendar has no such holiday in the
            month (entry and batch routes: a National Day Bonus recorded in
            October is a mistake worth stopping); ``False`` skips the row
            (back-filling, where refusing would block an unrelated save).
        """
        occasions = dict(HOLIDAY_OCCASIONS)
        days = {}
        by_day = defaultdict(lambda: self.browse())
        for rec in self:
            batch = rec.batch_id
            occasion = batch.component_id.x_paid_day
            if occasion not in occasions or not batch.period:
                continue
            key = (occasion, batch.period)
            if key not in days:
                days[key] = self._paid_day_date(occasion, batch.period)
            day = days[key]
            if not day:
                if strict:
                    raise UserError(_(
                        "%(component)s is paid for %(occasion)s, but Time "
                        "Off \u2192 Configuration \u2192 Public Holidays has "
                        "no %(occasion)s in %(month)s.\n\n"
                        "Check that the batch is for the right month. If it "
                        "is, ask HR to add the holiday there and set its "
                        "Pay Occasion to %(occasion)s.",
                        component=batch.component_id.name,
                        occasion=occasions[occasion],
                        month=batch.period.strftime('%B %Y')))
                continue
            if rec.date != day:
                by_day[day] |= rec
        for day, rows in by_day.items():
            # The system's own value, not an edit: past the draft and
            # import-only guards, but still through the constraints.
            super(KswPayEntry, rows).write({'date': day})
        return True

    # ------------------------------------------------------------------
    # Vacation hold
    # ------------------------------------------------------------------
    @api.depends('employee_id', 'date', 'period')
    def _compute_vacation_hold(self):
        """One hr.leave search per month, not one per row.

        A batch routinely carries forty entries; asking the question per
        record would be forty searches every time the Entries tab renders.
        """
        by_period = defaultdict(lambda: self.env['ksw.pay.entry'])
        for rec in self:
            rec.x_vacation_hold = False
            if rec.employee_id and rec.period:
                by_period[rec.period] |= rec
        for period, entries in by_period.items():
            holds = vacation_holds(self.env, entries.employee_id, period)
            for rec in entries:
                if rec.x_vacation_payslip_id:
                    # Already paid on the vacation: nothing to remove and
                    # nothing to release, so no warning either —
                    # x_vacation_settlement says what happened.
                    continue
                hold = holds.get(rec.employee_id.id)
                # Shown when the row is refused, and also on an undated row
                # in the month he came back — that one is allowed through
                # (there is no day to compare) but it is exactly the row
                # where a full month's meals get billed for half a month.
                # A Friday count is the exception: entry_blocked already
                # measured it against the Fridays after his return, so
                # within that it has nothing left to warn about.
                undated_unknown = not rec.date and (
                    rec.component_id.x_paid_day != 'friday')
                if hold and (entry_blocked(hold, rec) or undated_unknown):
                    rec.x_vacation_hold = hold_reason(self.env, hold)

    @api.depends('x_vacation_payslip_id')
    def _compute_vacation_settlement(self):
        for rec in self:
            slip = rec.x_vacation_payslip_id
            rec.x_vacation_settlement = _(
                "Paid on %(slip)s (%(leave)s)",
                slip=slip.number or slip.name,
                leave=slip.x_leave_id.holiday_status_id.display_name or '',
            ) if slip else False

    def _check_not_settled(self, what):
        """A paid entry is history: its figure is what left the bank.

        Deliberately NOT exempting env.su, unlike the other guards here — a
        sudo()'d import or cleanup rewriting it would silently change a
        payment already made. The settlement itself passes the
        ``ksw_vacation_settling`` context key.
        """
        if self.env.context.get('ksw_vacation_settling'):
            return
        settled = self.filtered('x_vacation_payslip_id')
        if not settled:
            return
        entry = settled[0]
        raise UserError(_(
            "%(entry)s (%(month)s) was paid on the vacation payslip "
            "%(slip)s. %(what)s is not possible. If that payslip was wrong, "
            "cancel or recompute it from the leave request first.",
            entry=entry.display_name,
            month=entry.period.strftime('%B %Y') if entry.period else '',
            slip=entry.sudo().x_vacation_payslip_id.display_name,
            what=what))

    def _check_not_paid_twice(self, payslip_id):
        """The last line against paying one commission twice: an entry may
        be stamped as paid on a vacation payslip only if nothing else has
        paid it — no other payslip, and no monthly run marked Paid with a
        line for him that month (``_months_paid_to``).

        Deliberately NOT exempting env.su or ``ksw_vacation_settling``: the
        settlement is the very caller this exists to stop when it is stale.
        """
        elsewhere = self.filtered(
            lambda e: e.x_vacation_payslip_id
            and e.x_vacation_payslip_id.id != payslip_id)
        Run = self.env['ksw.pay.run']
        committed = self.browse()
        for employee in self.employee_id:
            mine = self.filtered(lambda e, emp=employee: e.employee_id == emp)
            months = Run._months_paid_to(employee, set(mine.mapped('period')))
            committed |= mine.filtered(lambda e, m=months: e.period in m)
        bad = elsewhere | committed
        if bad:
            raise UserError(_(
                "These commission entries have already been paid, so they "
                "cannot be paid again on a vacation payslip:\n%(rows)s\n\n"
                "Recompute the vacation payslip from the leave request.",
                rows='\n'.join(
                    '\u2022 %s \u2014 %s' % (
                        e.display_name, e.period.strftime('%B %Y'))
                    for e in bad)))

    def _check_vacation_hold(self, what):
        """Server-side twin of the banner on the batch.

        Grouped by period for the same reason as the compute, and raising
        on the first offender: one entry is enough to make the answer no.
        """
        if self.env.su:
            return
        by_period = defaultdict(lambda: self.env['ksw.pay.entry'])
        for rec in self:
            if rec.employee_id and rec.period:
                by_period[rec.period] |= rec
        for period, entries in by_period.items():
            holds = vacation_holds(self.env, entries.employee_id, period)
            if not holds:
                continue
            for rec in entries:
                check_not_held(
                    self.env, rec.employee_id, period, what,
                    entry_date=rec.date, hold=holds.get(rec.employee_id.id),
                    entry=rec,
                )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    @api.constrains('quantity', 'component_id')
    def _check_quantity(self):
        for rec in self:
            if rec.component_id.calculation == 'fixed':
                continue
            if rec.quantity <= 0:
                # Zero is how people try to cancel a line, so the refusal
                # has to name the way that works, or the rejected value
                # stays in the form and every later save fails on it too.
                raise ValidationError(_(
                    "%(label)s must be greater than zero. To remove a line, "
                    "press Discard, then delete it with the bin icon at the "
                    "end of its row.",
                    label=rec.component_id.qty_label or _('Quantity')))

    @api.constrains('option_id', 'component_id')
    def _check_option(self):
        for rec in self:
            component = rec.component_id
            if not component:
                continue
            if component.has_options and not rec.option_id:
                raise ValidationError(_(
                    "Every %(name)s row has to say which one it is: "
                    "%(options)s.",
                    name=component.name,
                    options=', '.join(component.option_ids.mapped('name'))))
            if rec.option_id and rec.option_id.component_id != component:
                raise ValidationError(_(
                    "'%(option)s' is not one of %(name)s's choices.",
                    option=rec.option_id.name, name=component.name))

    @api.constrains('date', 'batch_id')
    def _check_date_in_period(self):
        import calendar
        for rec in self:
            if not rec.date or not rec.period:
                continue
            last = calendar.monthrange(rec.period.year, rec.period.month)[1]
            if not (rec.period <= rec.date <= rec.period.replace(day=last)):
                raise ValidationError(_(
                    "%(date)s is outside the batch period %(period)s.",
                    date=rec.date, period=rec.period.strftime('%B %Y')))

    @api.constrains('x_window_from', 'x_window_to', 'batch_id')
    def _check_window_in_period(self):
        for rec in self:
            if not (rec.x_window_from or rec.x_window_to):
                continue
            start, end = rec._month_bounds()
            if not (rec.x_window_from and rec.x_window_to
                    and start <= rec.x_window_from <= rec.x_window_to <= end):
                raise ValidationError(_(
                    "The days an entry covers must be a range inside "
                    "%(period)s.", period=rec.period.strftime('%B %Y')))

    # ------------------------------------------------------------------
    # Which days a row is about
    # ------------------------------------------------------------------
    def _month_bounds(self):
        self.ensure_one()
        start = self.period.replace(day=1)
        last = calendar.monthrange(start.year, start.month)[1]
        return start, start.replace(day=last)

    def _span(self):
        """The days this row is about, as ``(first, last)``, or ``None``.

        An occurrence is its own day; an imported figure is its window;
        an imported figure from before windows existed is the whole month.
        A typed monthly figure (a location allowance) has no days — it is
        about the month as a whole, and ``None`` says so.
        """
        self.ensure_one()
        if self.date:
            return self.date, self.date
        if self.x_window_from and self.x_window_to:
            return self.x_window_from, self.x_window_to
        if self.component_id.importer and self.period:
            return self._month_bounds()
        return None

    def _within(self, date_from, date_to):
        """Whether this row belongs to a sub-batch covering these days.

        A row with no days of its own (a monthly allowance) goes wherever
        its employee goes — the sub-batch takes all his components."""
        self.ensure_one()
        span = self._span()
        if span is None or not (date_from and date_to):
            return True
        return date_from <= span[0] and span[1] <= date_to

    def _band_scale(self):
        """How much of a month this row's tier bands are worth.

        A 15-day window earns through bands half as wide, the same way its
        free allowance is already pro-rated — otherwise a driver whose month
        is paid in two pieces climbs the ladder from the bottom twice and
        earns less than had it been paid once."""
        self.ensure_one()
        if not (self.x_window_from and self.x_window_to and self.period):
            return 1.0
        start, end = self._month_bounds()
        days = (self.x_window_to - self.x_window_from).days + 1
        return min(days / float((end - start).days + 1), 1.0)

    @api.depends('employee_id', 'option_id', 'date', 'x_window_from',
                 'x_window_to', 'x_sub_batch_id', 'batch_id.entry_ids')
    def _compute_overlap_note(self):
        """Name the other places this employee is paid for the same thing.

        One batch per component, department and month, so "the same thing"
        is always a sibling row in the same batch. Only rows that sit in a
        *different* sub-batch (or one in a sub-batch, one not) are compared
        — two rows typed side by side in the batch is ordinary data entry,
        not the double payment this exists to catch."""
        notes = {}
        for batch in self.batch_id:
            groups = defaultdict(list)
            for row in batch.entry_ids:
                if row.employee_id:
                    groups[(row.employee_id.id, row.option_id.id)].append(row)
            for rows in groups.values():
                if len(rows) < 2:
                    continue
                for row in rows:
                    where = []
                    for other in rows:
                        if other == row or \
                                other.x_sub_batch_id == row.x_sub_batch_id:
                            continue
                        if not row._overlaps(other):
                            continue
                        label = other.x_sub_batch_id.name or _(
                            'the batch itself')
                        if label not in where:
                            where.append(label)
                    if where:
                        notes[row.id] = _(
                            'Also in %(where)s', where=', '.join(where))
        for rec in self:
            rec.x_overlap_note = notes.get(rec.id, False)

    def _overlaps(self, other):
        mine, theirs = self._span(), other._span()
        if mine is None or theirs is None:
            # A monthly figure against anything for the same thing: the
            # month is paid twice.
            return True
        return mine[0] <= theirs[1] and theirs[0] <= mine[1]

    @api.constrains('date', 'component_id')
    def _check_date_required(self):
        for rec in self:
            if rec.component_id.needs_date and not rec.date:
                raise ValidationError(_(
                    "%(name)s is recorded per occurrence, so each entry needs "
                    "a date.", name=rec.component_id.name))

    @api.constrains('reason', 'component_id')
    def _check_reason_required(self):
        for rec in self:
            if rec.component_id.needs_reason and not (rec.reason or '').strip():
                raise ValidationError(_(
                    "%(name)s needs a reason on every entry.",
                    name=rec.component_id.name))

    # ------------------------------------------------------------------
    # CRUD — a submitted batch is closed to its supervisor
    # ------------------------------------------------------------------
    def _check_editable(self, what):
        if self.env.su:
            return
        for rec in self:
            batch = rec.batch_id
            check_period_unlocked(self.env, batch.period, what)
            if batch.state != 'draft':
                raise UserError(_(
                    "Batch %(name)s has been submitted. %(what)s is only "
                    "possible while it is in Draft.",
                    name=batch.name, what=what))
            # The batch can be open while this row is not: it went to the
            # General Manager in a sub-batch, or he approved it and returned
            # only the others.
            if rec.state != 'draft':
                raise UserError(_(
                    "%(entry)s is %(state)s — it is with the General Manager "
                    "or already approved. %(what)s is not possible; ask him "
                    "to return it.",
                    entry=rec.sudo().display_name,
                    state=dict(self._fields['state']._description_selection(
                        self.env)).get(rec.state), what=what))
            rec._check_import_only(what)

    def _check_import_only(self, what):
        """An imported figure is not the supervisor's to type over.

        The read-only Entries tab is cosmetic — anyone with write access
        can still reach these rows over RPC, which is exactly why this
        exists (Odoo 19 Pitfalls #15). One predicate, called from the one
        guard every route already goes through: create, write, unlink and
        Duplicate Line.

        The import itself comes through here too, so it announces itself
        with a context key rather than sudo(): the writes still have to
        pass the period lock, the draft-state check and the employee
        scope, which sudo() would wave through.
        """
        if self.env.context.get('ksw_pay_importing'):
            return
        component = self.batch_id.component_id
        if not component.entries_import_only:
            return
        if self.env.user.has_group('base.group_system'):
            return
        raise UserError(_(
            "%(component)s is imported and reviewed, not typed — "
            "%(what)s is not possible here. Press Import to refresh the "
            "figures. If one of them is still wrong, the cause is in the "
            "source data and not in this batch: contact technical support.",
            component=component.name, what=what))

    # ------------------------------------------------------------------
    # Fast entry — each new row starts as a copy of the last
    # ------------------------------------------------------------------
    # SAP's PA70 calls this "create with proposal" and Oracle's Batch
    # Element Entry calls it defaulting: when you are typing forty rows that
    # differ in one or two cells, the machine should carry the rest forward.
    # Doing it in default_get means the plain "Add a line" already produces a
    # copy — no shortcut to learn, nothing to click.
    _COPY_FORWARD = (
        'employee_id', 'option_id', 'date', 'quantity', 'quantity_ref',
        'threshold_qty', 'location_id', 'reason', 'details',
    )

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        batch_id = self.env.context.get('default_batch_id')
        if not batch_id:
            return vals
        previous = self.search(
            [('batch_id', '=', batch_id)], order='id desc', limit=1)
        if not previous:
            return vals
        for field in self._COPY_FORWARD:
            if field not in fields_list or vals.get(field):
                continue
            value = previous[field]
            if isinstance(value, models.Model):
                value = value.id
            if value:
                vals[field] = value
        return vals

    def action_duplicate_line(self):
        """Copy this line, for when the next one is nearly the same."""
        self.ensure_one()
        self._check_editable(_("Adding an entry"))
        self.copy()
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def _check_employee_allowed(self):
        """Server-side twin of the ``employee_id`` domain.

        A narrowed picker is cosmetic — this is what stops an RPC call
        recording pay for somebody else's staff.
        """
        if self.env.su or self.env.user.has_group(
                'KSW_commissions.group_commission_officer'):
            return
        allowed_by_batch = {}
        for rec in self:
            batch = rec.batch_id
            if batch.id not in allowed_by_batch:
                allowed_by_batch[batch.id] = batch._allowed_employees()
            if rec.employee_id not in allowed_by_batch[batch.id]:
                raise UserError(_(
                    "%(employee)s is not in %(scope)s and does not report to "
                    "you, so you cannot record pay for them.",
                    employee=rec.employee_id.sudo().display_name,
                    scope=batch.department_id.name or batch.site_id.name
                    or _('your departments')))

    @api.model_create_multi
    def create(self, vals_list):
        # A row can only reach a submitted or approved batch through
        # sudo() (a data fix, a script) — _check_editable refuses everyone
        # else. It joins the batch as the batch stands, which is what the
        # old related `state` did, rather than as a stray draft inside it.
        batch_states = dict(self.env['ksw.pay.batch'].sudo().browse(
            {v['batch_id'] for v in vals_list if v.get('batch_id')}
        ).mapped(lambda b: (b.id, b.state)))
        for vals in vals_list:
            if 'state' not in vals and batch_states.get(
                    vals.get('batch_id'), 'draft') != 'draft':
                vals['state'] = batch_states[vals['batch_id']]
        entries = super().create(vals_list)
        entries._check_editable(_("Adding an entry"))
        entries._apply_paid_day()
        entries._check_employee_allowed()
        entries._check_vacation_hold(_("Adding an entry"))
        entries._join_open_sub_batch()
        return entries

    def write(self, vals):
        if set(vals) - {'x_vacation_payslip_id'}:
            self._check_not_settled(_("Editing it"))
        if vals.get('x_vacation_payslip_id'):
            self._check_not_paid_twice(vals['x_vacation_payslip_id'])
        self._check_editable(_("Editing an entry"))
        res = super().write(vals)
        if 'date' in vals:
            # A holiday bonus keeps its holiday, whatever was sent.
            self._apply_paid_day()
        if 'employee_id' in vals:
            self._check_employee_allowed()
        # Moving a row's day can move it into or out of a sub-batch's range.
        if {'employee_id', 'date', 'x_window_from', 'x_window_to'} & set(vals):
            self._join_open_sub_batch()
        # `date` too: moving an occurrence back into the vacation is the
        # same act as typing it there. `quantity` for an undated Friday
        # count, which is measured against the Fridays after a return.
        if {'employee_id', 'date', 'quantity'} & set(vals):
            self._check_vacation_hold(_("Editing an entry"))
        return res

    def unlink(self):
        self._check_not_settled(_("Deleting it"))
        # Not exempting env.su: an approved row is a decision the General
        # Manager made, and no cleanup should make it disappear. Returning
        # it to draft first is the way out, and that has its own guards.
        approved = self.filtered(lambda e: e.state == 'approved')
        if approved:
            raise UserError(_(
                "%(entry)s has been approved by the General Manager and "
                "cannot be deleted.", entry=approved[0].sudo().display_name))
        self._check_editable(_("Deleting an entry"))
        return super().unlink()

    # ------------------------------------------------------------------
    # Workflow primitives — the entry is the unit of approval
    # ------------------------------------------------------------------
    # Every approval route (a batch, a department handover, a sub-batch,
    # the GM's review wizard) moves rows through these four and then calls
    # _sync_containers. One place decides what may move; the containers
    # only summarise.
    def _join_open_sub_batch(self):
        """A row typed for somebody in an open sub-batch belongs to it, so
        submitting the sub-batch takes everything he is owed so far."""
        SubBatch = self.env['ksw.pay.sub.batch'].sudo()
        for rec in self.sudo():
            if rec.state != 'draft':
                continue
            submission = rec.batch_id.submission_id
            target = SubBatch
            if submission and rec.employee_id:
                target = SubBatch.search([
                    ('submission_id', '=', submission.id),
                    ('batch_id', 'in', [rec.batch_id.id, False]),
                    ('state', 'in', ('draft', 'returned')),
                    ('employee_ids', 'in', [rec.employee_id.id]),
                ], limit=1)
                # Only a row about the sub-batch's own days: overtime on
                # the 20th is not part of a handover for the 1st–12th.
                if target and not rec._within(target.date_from,
                                              target.date_to):
                    target = SubBatch
            if rec.x_sub_batch_id != target and (
                    target or rec.x_sub_batch_id.state in ('draft', 'returned')):
                rec.x_sub_batch_id = target

    def _handed_over(self):
        """The submitted rows the General Manager actually has in front of
        him: sent in a sub-batch, or part of a department handed over.

        A batch submitted on its own is only a supervisor saying he has
        finished typing it — nobody is waiting on the GM for that.
        """
        return self.filtered(lambda e: e.state == 'submitted' and (
            e.x_sub_batch_id.submitted_date
            or e.batch_id.handed_over_date
            or e.batch_id.submission_id.state not in (False, 'draft')))

    def _sub_batch_controlled(self):
        """Rows a sub-batch handed over. A batch-level Reopen or Submit
        leaves them alone — the sub-batch and the GM decide them."""
        return self.filtered(
            lambda e: e.state != 'draft' and e.x_sub_batch_id.submitted_date)

    def _paid_entries(self):
        """Rows whose money has left: paid on a vacation payslip, or in a
        month the accountant marked Paid. Nobody reopens these."""
        periods = list(set(self.mapped('period')))
        paid_periods = set(self.env['ksw.pay.run'].sudo().search([
            ('period', 'in', periods), ('state', '=', 'paid'),
        ]).mapped('period')) if periods else set()
        # A run marked paid one bank account at a time: his own line.
        paid_lines = set(
            (l.period, l.employee_id.id)
            for l in self.env['ksw.pay.run.line'].sudo().search([
                ('period', 'in', periods), ('x_paid', '=', True),
                ('employee_id', 'in', self.employee_id.ids),
            ])) if periods else set()
        return self.filtered(
            lambda e: e.x_vacation_payslip_id or e.period in paid_periods
            or (e.period, e.employee_id.id) in paid_lines)

    def _check_reopenable(self):
        """Refuse to send an approved row back when it has been paid, or a
        vacation settlement has already counted on it."""
        paid = self._paid_entries()
        if paid:
            raise UserError(_(
                "%(entry)s (%(month)s) has been paid, so it cannot be "
                "reopened — not even by the General Manager.",
                entry=paid[0].sudo().display_name,
                month=paid[0].period.strftime('%B %Y')))
        latched = self.env['ksw.leave.commission.entry'].sudo().search([
            ('entry_id', 'in', self.ids), ('included', '=', True),
            ('leave_id.state', 'not in', ('refuse', 'cancel')),
        ], limit=1)
        if latched:
            raise UserError(_(
                "%(entry)s is already included in the vacation settlement "
                "of %(leave)s. Return that request to Accounting and refresh "
                "its commissions without it before reopening this row.",
                entry=latched.entry_id.sudo().display_name,
                leave=latched.leave_id.sudo().display_name))

    def _check_not_held_for_submit(self, what):
        """Refuse to hand over rows that pay a settled vacation twice."""
        if self.env.su:
            return
        entries = self.filtered('employee_id')
        by_period = defaultdict(lambda: self.env['ksw.pay.entry'])
        for rec in entries:
            by_period[rec.period] |= rec
        held = self.env['ksw.pay.entry']
        for period, rows in by_period.items():
            holds = vacation_holds(self.env, rows.employee_id, period)
            held |= rows.filtered(
                lambda e: not e.x_vacation_payslip_id
                and entry_blocked(holds.get(e.employee_id.id), e))
        if held:
            raise UserError(_(
                "%(what)s is not possible: %(who)s went on vacation and "
                "what they had earned was settled on the leave request. "
                "Delete those entries, or ask the General Manager to release "
                "the month (Commissions → Vacation Releases).",
                what=what,
                who=', '.join(held.employee_id.sudo().mapped('display_name'))))

    def _wf_submit(self):
        todo = self.filtered(lambda e: e.state == 'draft')
        todo.sudo().write({'state': 'submitted', 'x_return_reason': False})
        return todo

    def _wf_approve(self):
        todo = self.filtered(lambda e: e.state == 'submitted')
        todo.sudo().write({'state': 'approved', 'x_return_reason': False})
        return todo

    def _wf_return(self, reason=None):
        """submitted → draft. With a reason when the GM refused it; without
        one when the supervisor took it back before anyone looked."""
        todo = self.filtered(lambda e: e.state == 'submitted')
        todo.sudo().write({'state': 'draft',
                           'x_return_reason': reason or False})
        return todo

    def _wf_reopen(self, reason):
        """approved → draft, by the General Manager, while nothing is paid."""
        todo = self.filtered(lambda e: e.state == 'approved')
        todo._check_reopenable()
        todo.sudo().write({'state': 'draft', 'x_return_reason': reason})
        return todo

    def _sync_containers(self):
        """Re-derive the batch, sub-batch and department summaries."""
        entries = self.sudo()
        entries.batch_id._sync_state_from_entries()
        entries.x_sub_batch_id._sync_state_from_entries()
        entries.batch_id.submission_id._sync_state_from_entries()
        return True
