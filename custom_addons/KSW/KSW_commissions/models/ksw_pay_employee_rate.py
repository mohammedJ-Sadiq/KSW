"""KSW Employee Pay Rate — what one employee's unit of a component is worth.

A pay component carries one rate: a Friday is 100, a lunch is 20. That rate is
the *policy*, and a policy has exceptions — this employee's Friday is 150.

SAP records the same thing as an amount on the employee's own wage-type record
(infotype 0014/0015) rather than on the wage type; Oracle Fusion as an input
value at assignment level overriding the element's rate definition. Both keep
the catalog entry intact and hang the exception off the person, which is what
this model does: the component still says 100 and always will, and an exception
row says *this* employee, *from this date*, gets 150.

Three properties it has to have, and the reasoning for each:

**Dated.** An exception is money, and money has a history. Raising a rate in
March must leave January and February computing at the old one — so a row has
``date_from`` / ``date_to`` and the resolver picks by the entry's own date, not
by today. Rows for the same employee, component and option may not overlap, or
"the rate" would be a question with two answers.

**Resolved most-specific-first.** An option-specific row (this employee's lunch)
beats a component-wide one (this employee's meals, whichever meal); within the
same specificity the later ``date_from`` wins. Same shape as
``ksw.sales.commission.rule._resolve_rule``, which already does this for sales.

**Not editable by the person who records the entries.** The supervisor types
the quantity; what a unit is worth is an Administrator's decision. That is the
same separation of duties that keeps General Manager from implying Supervisor —
it is the whole reason this is a configuration record and not a field on the
entry. The ad-hoc "pay something else just this once" answer already exists as
``ksw.pay.entry.amount_override``.

A rate change never touches a closed month: :meth:`_recompute_affected_entries`
only re-runs entries whose batch is still in draft and whose period is not
locked.
"""
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .ksw_commission_lock import period_is_locked


class KswPayEmployeeRate(models.Model):
    _name = 'ksw.pay.employee.rate'
    _description = 'KSW Employee Pay Rate'
    _order = 'component_id, employee_id, date_from desc, id desc'

    employee_id = fields.Many2one(
        'hr.employee', string='Employee', required=True,
        ondelete='cascade', index=True,
    )
    component_id = fields.Many2one(
        'ksw.pay.component', string='Pay Component', required=True,
        ondelete='cascade', index=True,
        domain="[('calculation', '=', 'qty_rate')]",
        help='Only components paid as quantity × rate can have a '
             'per-employee rate — a salary-derived one already varies with '
             'the employee, and a fixed amount is typed on the entry.',
    )
    option_id = fields.Many2one(
        'ksw.pay.option', string='Type', ondelete='cascade', index=True,
        domain="[('component_id', '=', component_id)]",
        help="Which of the component's choices this rate is for — this "
             "employee's lunch. Leave empty and it applies to every choice.",
    )
    has_options = fields.Boolean(
        related='component_id.has_options', readonly=True)
    qty_label = fields.Char(
        related='component_id.qty_label', readonly=True)

    rate = fields.Float(
        string='Rate', digits=(16, 4), required=True,
        help='What one unit is worth for this employee — 150.00 per Friday '
             'where the standard is 100.00.',
    )
    standard_rate = fields.Float(
        string='Standard Rate', digits=(16, 4),
        compute='_compute_standard_rate',
        help='What the component pays everybody else. Shown so the exception '
             'can be read as an exception.',
    )

    date_from = fields.Date(
        string='Valid From', required=True,
        default=lambda s: fields.Date.context_today(s).replace(day=1),
        help='The first month this rate applies to. Entries dated before it '
             'keep computing at the previous rate.',
    )
    date_to = fields.Date(
        string='Valid To', help='Leave empty to apply indefinitely.',
    )
    reason = fields.Char(
        help='Why this employee is an exception — kept for whoever asks '
             'about it later.',
    )
    active = fields.Boolean(default=True)

    department_id = fields.Many2one(
        related='employee_id.department_id', store=True, readonly=True,
    )

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------
    @api.depends('employee_id', 'component_id', 'option_id', 'rate')
    def _compute_display_name(self):
        for rec in self:
            what = rec.option_id.name or rec.component_id.name or ''
            rec.display_name = '%s — %s @ %.2f' % (
                rec.employee_id.display_name or '', what, rec.rate or 0.0)

    @api.depends('component_id', 'component_id.rate', 'option_id',
                 'option_id.rate')
    def _compute_standard_rate(self):
        for rec in self:
            component = rec.component_id
            if not component:
                rec.standard_rate = 0.0
            elif rec.option_id and rec.option_id.component_id == component:
                rec.standard_rate = rec.option_id.rate or 0.0
            else:
                rec.standard_rate = component.rate or 0.0

    # ------------------------------------------------------------------
    # The resolver — the only reason this model exists
    # ------------------------------------------------------------------
    @api.model
    def _rate_for(self, employee, component, option=None, date=None):
        """Return this employee's own rate row, or an empty recordset.

        ``sudo()``: the figure has to come out the same for everybody who
        reads the entry, and a supervisor holds read on the catalog but is
        not the audience for the exception list.
        """
        if not employee or not component:
            return self.browse()
        if component.calculation != 'qty_rate':
            return self.browse()

        date = fields.Date.to_date(date) or fields.Date.context_today(self)
        domain = [
            ('employee_id', '=', employee.id),
            ('component_id', '=', component.id),
            ('date_from', '<=', date),
            '|', ('date_to', '=', False), ('date_to', '>=', date),
        ]
        if option and option.component_id == component:
            domain += ['|', ('option_id', '=', option.id),
                       ('option_id', '=', False)]
        else:
            domain += [('option_id', '=', False)]

        rows = self.sudo().search(domain)
        if not rows:
            return rows
        # Most specific first: an option-specific row beats a component-wide
        # one; among equals the later start date wins. The overlap constraint
        # means this only ever has to break a tie between those two kinds.
        return rows.sorted(
            key=lambda r: (bool(r.option_id), r.date_from, r.id),
            reverse=True,
        )[:1]

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    @api.constrains('rate')
    def _check_rate(self):
        for rec in self:
            if rec.rate < 0:
                raise ValidationError(_("A rate cannot be negative."))

    @api.constrains('date_from', 'date_to')
    def _check_dates(self):
        for rec in self:
            if rec.date_to and rec.date_from and rec.date_to < rec.date_from:
                raise ValidationError(_(
                    "'Valid To' comes before 'Valid From' on %(name)s.",
                    name=rec.display_name))

    @api.constrains('component_id', 'option_id')
    def _check_component(self):
        for rec in self:
            if rec.component_id.calculation != 'qty_rate':
                raise ValidationError(_(
                    "%(name)s is not paid as quantity × rate, so it has no "
                    "unit rate to override. A salary-derived component "
                    "already varies with the employee; a fixed amount is "
                    "typed on the entry itself.",
                    name=rec.component_id.name))
            if rec.option_id and rec.option_id.component_id != rec.component_id:
                raise ValidationError(_(
                    "'%(option)s' is not one of %(name)s's choices.",
                    option=rec.option_id.name, name=rec.component_id.name))

    @api.constrains('employee_id', 'component_id', 'option_id',
                    'date_from', 'date_to', 'active')
    def _check_no_overlap(self):
        """Two rows may not answer the same question for the same day.

        Without this the resolver would be picking one of two equally valid
        rates, and which one it picked would depend on the order rows were
        created in.
        """
        for rec in self:
            if not rec.active:
                continue
            overlapping = self.search([
                ('id', '!=', rec.id),
                ('employee_id', '=', rec.employee_id.id),
                ('component_id', '=', rec.component_id.id),
                ('option_id', '=', rec.option_id.id or False),
                ('date_from', '<=', rec.date_to or '9999-12-31'),
                '|', ('date_to', '=', False),
                ('date_to', '>=', rec.date_from),
            ], limit=1)
            if overlapping:
                raise ValidationError(_(
                    "%(employee)s already has a rate for %(what)s starting "
                    "%(start)s, and the two periods overlap. Close that one "
                    "off first — two rates for the same day is a question "
                    "with two answers.",
                    employee=rec.employee_id.display_name,
                    what=(rec.option_id.name or rec.component_id.name),
                    start=overlapping.date_from))

    # ------------------------------------------------------------------
    # Keeping open entries in step
    # ------------------------------------------------------------------
    def _affected_entries(self):
        """Entries this rate row could change.

        Only **draft** entries in an **unlocked** period: an approved month
        has been signed off and, once exported, paid. Changing a rate today
        must not silently restate what was already paid, and the resolver's
        date window is what keeps history right in the first place.
        """
        Entry = self.env['ksw.pay.entry'].sudo()
        entries = Entry.browse()
        for rec in self:
            domain = [
                ('employee_id', '=', rec.employee_id.id),
                ('component_id', '=', rec.component_id.id),
                ('state', '=', 'draft'),
            ]
            if rec.option_id:
                domain.append(('option_id', '=', rec.option_id.id))
            entries |= Entry.search(domain)
        return entries.filtered(
            lambda e: not period_is_locked(self.env, e.period))

    def _mark_for_recompute(self, entries):
        """Queue ``entries`` to have their amount worked out again.

        A stored compute cannot depend on this model — there is no relation
        from an entry to a rate row to traverse — so the trigger is explicit.
        """
        if not entries:
            return entries
        Entry = self.env['ksw.pay.entry']
        for name in ('rate', 'amount_computed', 'amount', 'is_overridden'):
            self.env.add_to_compute(Entry._fields[name], entries)
        return entries

    def _recompute_affected_entries(self):
        return self._mark_for_recompute(self._affected_entries())

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._recompute_affected_entries()
        return records

    def write(self, vals):
        # Both sides: the entries this row reaches now — which have to fall
        # back to the standard rate if the change moves it off them — and the
        # ones it reaches afterwards. The marking has to happen *after* the
        # write, or the flush inside it recomputes them from the old values
        # and consumes the mark.
        before = self._affected_entries()
        res = super().write(vals)
        self._mark_for_recompute(before.exists() | self._affected_entries())
        return res

    def unlink(self):
        # Captured while the row still exists, marked once it is gone:
        # unlink() flushes before deleting, so marking first would recompute
        # the entries at the rate that is about to disappear.
        affected = self._affected_entries()
        res = super().unlink()
        self._mark_for_recompute(affected.exists())
        return res
