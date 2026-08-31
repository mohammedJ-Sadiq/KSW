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
from markupsafe import Markup

from odoo import _, SUPERUSER_ID, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .ksw_commission_lock import check_period_unlocked, period_is_locked

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

    submitted_by = fields.Many2one('res.users', readonly=True, copy=False)
    submitted_date = fields.Datetime(readonly=True, copy=False)
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
    needs_location = fields.Boolean(
        related='component_id.needs_location', readonly=True)
    importer = fields.Selection(
        related='component_id.importer', readonly=True)

    entry_count = fields.Integer(compute='_compute_totals', store=True)
    employee_count = fields.Integer(compute='_compute_totals', store=True)
    total_quantity = fields.Float(
        compute='_compute_totals', store=True, digits=(16, 2))
    total_amount = fields.Monetary(compute='_compute_totals', store=True)

    is_locked = fields.Boolean(compute='_compute_is_locked')
    x_can_reopen = fields.Boolean(compute='_compute_can_reopen')

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
            rec.total_amount = sum(rec.entry_ids.mapped('amount'))

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
            if rec.state == 'draft' or (locked[rec.period] and not is_gm):
                rec.x_can_reopen = False
                continue
            if rec.state == 'approved':
                rec.x_can_reopen = is_gm
                continue
            rec.x_can_reopen = is_gm or rec.submission_id.state != 'submitted'

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
            departments = self._allowed_departments()
            in_scope = Employee.search(
                [('department_id', 'in', departments.ids)]
            ) if departments else Employee.browse()

        if privileged:
            return in_scope
        return in_scope | self._subordinate_employees()

    @api.depends_context('uid')
    @api.depends('department_id', 'site_id')
    def _compute_allowed_employees(self):
        for rec in self:
            rec.allowed_employee_ids = rec._allowed_employees()

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
        return batches

    def write(self, vals):
        if vals.get('period'):
            vals['period'] = fields.Date.to_date(vals['period']).replace(day=1)
        if vals.get('component_id'):
            self._clear_unused_scope(vals)
        if not self.env.su:
            protected = set(vals) - {
                'state', 'note', 'submitted_by', 'submitted_date',
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
        res = super().write(vals)
        if 'component_id' in vals:
            self._check_component_rights()
        if 'department_id' in vals:
            self._check_department_rights()
        if {'period', 'department_id', 'site_id'} & set(vals):
            self._ensure_submission()
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

    def unlink(self):
        for rec in self:
            check_period_unlocked(
                self.env, rec.period, _("Deleting this batch"))
            if rec.state != 'draft' and not self.env.su:
                raise UserError(_(
                    "Only a draft batch can be deleted. %(name)s is %(state)s.",
                    name=rec.name, state=rec.state))
        return super().unlink()

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
            rec._ensure_submission()
            rec.write({
                'state': 'submitted',
                'submitted_by': self.env.uid,
                'submitted_date': fields.Datetime.now(),
                'return_reason': False,
            })
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
            rec.write({
                'state': 'draft',
                'return_reason': reason or False,
                'submitted_by': False,
                'submitted_date': False,
            })
            partners = rec.submitted_by.partner_id
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
            if self.env.su or is_gm:
                rec.write({'state': 'draft', 'submitted_by': False,
                           'submitted_date': False})
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
            rec.write({'state': 'draft', 'submitted_by': False,
                       'submitted_date': False})
        self.mapped('submission_id.run_id')._refresh_register()
        return True

    # ------------------------------------------------------------------
    # Entry helpers
    # ------------------------------------------------------------------
    def action_add_recurring(self):
        """Materialise this component's recurring entries into the batch."""
        self.ensure_one()
        Recurring = self.env['ksw.pay.recurring']
        created = Recurring._apply_to_batch(self)
        return self._notify(_(
            "%(count)s recurring entr%(plural)s added.",
            count=len(created), plural=_('y') if len(created) == 1 else _('ies'),
        ))

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
        return getattr(self, method)()

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
    state = fields.Selection(related='batch_id.state', store=True,
                             readonly=True, index=True)
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
    location_id = fields.Many2one('ksw.site', string='Location')
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

    @api.depends('employee_id', 'quantity', 'threshold_qty', 'amount_override',
                 'component_id', 'component_id.calculation',
                 'component_id.rate', 'component_id.divisor',
                 'component_id.factor', 'component_id.tier_ids.rate',
                 'component_id.tier_ids.width', 'site_id',
                 'option_id', 'option_id.rate')
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
                 'employee_id')
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
        )

        if self.quantity_ref and component.qty_ref_label:
            notes.insert(0, '%s: %s' % (
                component.qty_ref_label,
                self.env['ir.qweb.field.float'].value_to_html(
                    self.quantity_ref, {'precision': 2}),
            ))

        currency = self.currency_id.symbol or ''
        html = ['<table class="table table-sm o_main_table">',
                '<thead><tr>',
                '<th>%s</th>' % _('Step'),
                '<th class="text-end">%s</th>' % (component.qty_label or _('Quantity')),
                '<th class="text-end">%s</th>' % _('Rate'),
                '<th class="text-end">%s</th>' % _('Amount'),
                '</tr></thead><tbody>']
        for row in rows:
            html.append(
                '<tr><td>%s</td>'
                '<td class="text-end">%s</td>'
                '<td class="text-end">%s</td>'
                '<td class="text-end">%s</td></tr>' % (
                    row['label'],
                    ('%.2f' % row['quantity']) if row.get('quantity') is not None else '',
                    ('%.4f' % row['rate']) if row.get('rate') is not None else '',
                    ('%.2f' % row['amount']) if row.get('amount') is not None else '',
                ))
        html.append(
            '<tr class="fw-bold border-top">'
            '<td>%s</td><td></td><td></td>'
            '<td class="text-end">%s %s</td></tr>' % (
                _('Total'), '%.2f' % (self.amount or 0.0), currency))
        html.append('</tbody></table>')

        if self.is_overridden:
            notes.append(_(
                'The computed amount was overridden by hand '
                '(computed: %(computed).2f).', computed=self.amount_computed))
        if self.details:
            notes.append(self.details)
        if notes:
            html.append('<ul class="mb-0">')
            html.extend('<li>%s</li>' % note for note in notes)
            html.append('</ul>')
        return ''.join(html)

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
    # Validation
    # ------------------------------------------------------------------
    @api.constrains('quantity', 'component_id')
    def _check_quantity(self):
        for rec in self:
            if rec.component_id.calculation == 'fixed':
                continue
            if rec.quantity <= 0:
                raise ValidationError(_(
                    "%(label)s must be greater than zero.",
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
        entries = super().create(vals_list)
        entries._check_editable(_("Adding an entry"))
        entries._check_employee_allowed()
        return entries

    def write(self, vals):
        self._check_editable(_("Editing an entry"))
        res = super().write(vals)
        if 'employee_id' in vals:
            self._check_employee_allowed()
        return res

    def unlink(self):
        self._check_editable(_("Deleting an entry"))
        return super().unlink()
