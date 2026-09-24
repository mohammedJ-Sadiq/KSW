"""KSW Pay Component — the catalog of things an employee can be paid extra for.

This is the SAP *wage type* / Oracle *element* of this module: a pay type is a
**configuration record**, not a model. Overtime, driver trips, breakfast, a
mobile-phone allowance and a holiday bonus are all rows in this table, and
adding a new one costs no code, no menu, no access group and no record rule.

Everything reduces to ``quantity x rate -> amount``. Four calculation methods
cover every case KSW has:

``fixed``
    The supervisor types the amount. Allowances, bonuses.
``qty_rate``
    ``quantity x rate``, the rate configured here. Meals (count x price),
    Friday allowance (days x rate).
``wage_rate``
    ``quantity x (basic salary / divisor x factor)``. Overtime is hours with
    divisor 240 and factor 1.5, per Saudi Labour Law art. 107.
``tiered``
    A waterfall over :class:`KswPayRateTier`, optionally per site, with a
    per-entry free threshold. Driver trips.

A component may also carry **options** (:class:`KswPayComponentOption`) — the
same kind of pay with more than one unit rate. Meals are one component with
Breakfast, Lunch and Dinner inside it, not three components: a supervisor
records the month's meals on one screen and picks the meal on each row.

Two rules learned the hard way and encoded here:

* The amount is computed in **one unrounded expression**. Never
  ``rate x quantity`` where ``rate`` is a Monetary — 4500/240x1.5 = 28.125
  rounds to 28.13 and pays 112.52 for four hours instead of 112.50.
* Reading ``hr.version.wage`` needs ``sudo()``; it is group-restricted, and a
  supervisor without HR rights would otherwise hit an AccessError just opening
  the entry form.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

CALCULATION = [
    ('fixed', 'Fixed amount'),
    ('qty_rate', 'Quantity × rate'),
    ('wage_rate', 'Quantity × salary-derived rate'),
    ('tiered', 'Tiered on quantity'),
]

# 'site' was retired in 19.0.4.3.0: Driver Trips, the only component that
# ever used it, is recorded per department now. Historical batches keep
# their ksw.pay.batch.site_id as a record of where those trips were driven
# — the value is read nowhere, and the option is gone so nothing can put
# the site picker back in front of a supervisor.
SCOPE = [
    ('department', 'Department'),
    ('company', 'Company-wide'),
]


class KswPayComponent(models.Model):
    _name = 'ksw.pay.component'
    _description = 'KSW Pay Component'
    _order = 'sequence, code'

    name = fields.Char(required=True, translate=True)
    code = fields.Char(
        required=True,
        help='Stable identifier used in exports and reports.',
    )
    kind = fields.Selection(
        [('earning', 'Earning'), ('deduction', 'Deduction')],
        default='earning', required=True,
    )
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    description = fields.Text()

    # ------------------------------------------------------------------
    # Calculation
    # ------------------------------------------------------------------
    calculation = fields.Selection(
        CALCULATION, default='fixed', required=True,
        help='How the amount is worked out from the quantity.',
    )
    rate = fields.Float(
        digits=(16, 4),
        help='Unit rate for "Quantity × rate" — e.g. 20.00 per lunch.',
    )
    divisor = fields.Float(
        digits=(16, 2), default=240.0,
        help='For "Quantity × salary-derived rate": monthly hours the basic '
             'salary is divided by. KSW uses 240.',
    )
    factor = fields.Float(
        digits=(16, 4), default=1.5,
        help='Multiplier on the salary-derived rate. Saudi Labour Law '
             'art. 107 sets overtime at 1.5.',
    )
    tier_ids = fields.One2many(
        'ksw.pay.rate.tier', 'component_id', string='Rate Tiers',
    )
    option_ids = fields.One2many(
        'ksw.pay.option', 'component_id', string='Options',
        help='Variants of the same pay, each with its own rate — Breakfast, '
             'Lunch, Dinner. Leave empty when the component has a single '
             'rate.',
    )
    employee_rate_ids = fields.One2many(
        'ksw.pay.employee.rate', 'component_id', string='Employee Rates',
        help='Employees whose unit of this component is worth something '
             'other than the rate above.',
    )
    has_options = fields.Boolean(
        compute='_compute_has_options', store=True,
        help='Set automatically: this component is recorded by choosing one '
             'of its options on every entry.',
    )

    # ------------------------------------------------------------------
    # What an entry looks like
    # ------------------------------------------------------------------
    qty_label = fields.Char(
        string='Quantity Label', default='Quantity', translate=True,
        help='What the quantity is called on the entry screen — Hours, '
             'Trips, Meals, Days.',
    )
    qty_ref_label = fields.Char(
        string='Reference Quantity Label', translate=True,
        help='Optional second figure recorded for justification but not used '
             'in the calculation — for driver trips, the raw trip count '
             'behind the weighted one. Leave empty to hide the column.',
    )
    needs_date = fields.Boolean(
        string='Per Occurrence', default=False,
        help='Tick when each entry is a dated occurrence (overtime worked on '
             'a given day). Leave off for a monthly total.',
    )
    needs_location = fields.Boolean(
        string='Ask for Location', default=False,
    )
    needs_reason = fields.Boolean(
        string='Ask for a Reason', default=False,
        help='Require a short justification on every entry.',
    )
    scope = fields.Selection(
        SCOPE, default='department', required=True,
        help='What a batch of this component covers.',
    )

    # ------------------------------------------------------------------
    # Who may enter it
    # ------------------------------------------------------------------
    entry_group_ids = fields.Many2many(
        'res.groups', 'ksw_pay_component_group_rel',
        'component_id', 'group_id', string='Restricted To',
        help='Leave empty for any commission supervisor. Set to restrict '
             'this component to particular roles — no new group has to exist '
             'unless you actually need the distinction.',
    )

    importer = fields.Selection(
        selection='_selection_importer', string='Import Source',
        help='Optional. Adds an Import button on batches of this component.',
    )
    # ------------------------------------------------------------------
    # BAS journal entry
    # ------------------------------------------------------------------
    # Which accounts this kind of pay hits when the month is exported as a
    # journal entry for BAS. They live on the component for the same reason
    # its rate does: a pay type is configuration, and the account it is
    # expensed to is part of what the type *is* — SAP hangs a symbolic
    # account off the wage type the same way. A new component therefore
    # carries its own accounts in, instead of needing a mapping table
    # somewhere else to be remembered.
    x_bas_expense_code = fields.Char(
        string='BAS Expense Account',
        help='The account this pay is expensed to in BAS — 3203020007. '
             'Debited.',
    )
    x_bas_expense_name = fields.Char(
        string='BAS Expense Account Name',
        help='The account name written into the entry, as the accountant '
             'reads it in BAS.',
    )
    x_bas_accrual_code = fields.Char(
        string='BAS Accrual Account',
        help='The account the money becomes owed on until it is paid — '
             '2107010001. Credited with whatever is left after the '
             "employee's loan installments.",
    )
    x_bas_accrual_name = fields.Char(string='BAS Accrual Account Name')
    x_bas_use_cost_center = fields.Boolean(
        string='Stamp the BAS Cost Centre', default=False,
        help="Write the employee's BAS cost centre (his truck, for a "
             'driver) on the expense line. Off for pay that is not '
             'attributable to one vehicle or site.',
    )

    entries_import_only = fields.Boolean(
        string='Filled by Import Only', default=False,
        help='The entries are produced by the import and reviewed, never '
             'typed: no line can be added, edited or deleted by hand. For a '
             'component whose figures come from another system, where a '
             'correction belongs in that system and not in this one.',
    )

    _unique_code = models.Constraint(
        'UNIQUE(code)', 'A pay component code must be unique.')

    @api.model
    def _selection_importer(self):
        """Importers other modules can plug in.

        Depends only on what is installed, never on the record — a dynamic
        selection that varies per record does not survive the web client
        (it strips the context and caches the payload).
        """
        return [('bas_trips', 'Driver trips from BAS')]

    @api.depends('name', 'code')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = '%s (%s)' % (rec.name, rec.code) \
                if rec.code else rec.name

    @api.depends('option_ids')
    def _compute_has_options(self):
        for rec in self:
            rec.has_options = bool(rec.option_ids)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    @api.constrains('calculation', 'option_ids')
    def _check_options(self):
        for rec in self:
            if rec.option_ids and rec.calculation != 'qty_rate':
                raise ValidationError(_(
                    "'%(name)s' has options, so its amount has to be a "
                    "quantity × rate — each option carries its own rate.",
                    name=rec.name))

    @api.constrains('entries_import_only', 'importer')
    def _check_import_only_has_an_importer(self):
        """Import-only without an importer is a batch nobody can fill.

        The flag closes the only other way in, so the button has to exist
        or the component is a dead end with no error to explain it.
        """
        for rec in self:
            if rec.entries_import_only and not rec.importer:
                raise ValidationError(_(
                    "'%(name)s' is set to be filled by import only, so it "
                    "needs an Import Source — otherwise its batches can "
                    "never be filled at all.", name=rec.name))


    @api.constrains('calculation', 'divisor', 'rate', 'tier_ids')
    def _check_calculation(self):
        for rec in self:
            if rec.calculation == 'wage_rate' and not rec.divisor:
                raise ValidationError(_(
                    "'%(name)s' derives its rate from the salary, so it needs "
                    "a divisor (KSW uses 240).", name=rec.name))
            if rec.calculation == 'tiered' and not rec.tier_ids:
                raise ValidationError(_(
                    "'%(name)s' is tiered but has no rate tiers configured.",
                    name=rec.name))

    # ------------------------------------------------------------------
    # The resolver — the whole point of this model
    # ------------------------------------------------------------------
    def _unit_rate(self, option=None, employee=None, date=None):
        """The per-unit rate for one entry.

        Three answers, most specific first:

        1. the employee's own rate, when one is on file for this component
           (and this option) on ``date`` — see :class:`ksw.pay.employee.rate`;
        2. the option's rate when the entry names one — a lunch is 20 and a
           breakfast 10 within the same Meals component;
        3. the component's single rate.

        An option belonging to a different component is ignored rather than
        trusted; the entry constraint rejects it, and a rate is not the place
        to find out.
        """
        self.ensure_one()
        # Most components never have an exception; reading the o2m once per
        # component (cached for the whole compute) is cheaper than a search
        # per entry to find nothing.
        if self.sudo().employee_rate_ids:
            own = self.env['ksw.pay.employee.rate']._rate_for(
                employee, self, option=option, date=date)
            if own:
                return own.rate or 0.0
        if option and option.component_id == self:
            return option.rate or 0.0
        return self.rate or 0.0

    def _resolve(self, employee, quantity=0.0, site=None, threshold=0.0,
                 option=None, date=None):
        """Return ``(rate, amount)`` for one entry.

        ``rate`` is informational and may be fractional; ``amount`` is the
        figure that gets paid and is computed **without** going through the
        rounded rate.
        """
        self.ensure_one()
        quantity = quantity or 0.0

        if self.calculation == 'fixed':
            # The supervisor types the amount; nothing to derive.
            return 0.0, 0.0

        if self.calculation == 'qty_rate':
            rate = self._unit_rate(option, employee=employee, date=date)
            return rate, rate * quantity

        if self.calculation == 'wage_rate':
            # sudo(): hr.version.wage is group-restricted.
            emp = employee.sudo() if employee else employee
            wage = (emp.current_version_id.wage if emp else 0.0) or 0.0
            if not self.divisor:
                return 0.0, 0.0
            rate = wage / self.divisor * (self.factor or 1.0)
            # One expression, deliberately not `rate * quantity`.
            amount = wage / self.divisor * (self.factor or 1.0) * quantity
            return rate, amount

        if self.calculation == 'tiered':
            return self._resolve_tiered(quantity, site=site,
                                        threshold=threshold)

        return 0.0, 0.0

    def _resolve_detail(self, employee, quantity=0.0, site=None,
                        threshold=0.0, option=None, date=None):
        """Return ``(rows, notes)`` explaining how the amount was reached.

        ``rows`` are dicts of ``label`` / ``quantity`` / ``rate`` / ``amount``
        — one per step of the derivation. ``notes`` are free-text caveats.
        This is what the entry's "how was this worked out" panel renders, and
        it is deliberately produced by the same method family that does the
        arithmetic, so the explanation can never drift from the figure.
        """
        self.ensure_one()
        quantity = quantity or 0.0
        rows, notes = [], []

        if self.calculation == 'fixed':
            notes.append(_('A fixed amount, entered by hand.'))
            return rows, notes

        if self.calculation == 'qty_rate':
            own = self.env['ksw.pay.employee.rate']._rate_for(
                employee, self, option=option, date=date) \
                if self.sudo().employee_rate_ids else None
            rate = self._unit_rate(option, employee=employee, date=date)
            named = option if option and option.component_id == self else None
            what = named.name if named else (self.qty_label or _('Quantity'))
            label = _("%(what)s at this employee's own rate", what=what) \
                if own else \
                _('%(what)s at the configured rate', what=what)
            rows.append({
                'label': label,
                'quantity': quantity,
                'rate': rate,
                'amount': rate * quantity,
            })
            if own:
                # A figure a supervisor cannot justify is a figure he cannot
                # defend at review: say that this one is an exception, what
                # everybody else gets, and since when.
                notes.append(_(
                    'This employee has a rate of their own: %(rate).2f '
                    'instead of the standard %(standard).2f, effective '
                    '%(since)s.',
                    rate=own.rate or 0.0,
                    standard=own.standard_rate or 0.0,
                    since=own.date_from))
                if own.reason:
                    notes.append(own.reason)
            return rows, notes

        if self.calculation == 'wage_rate':
            emp = employee.sudo() if employee else employee
            wage = (emp.current_version_id.wage if emp else 0.0) or 0.0
            hourly = (wage / self.divisor) if self.divisor else 0.0
            rows.append({
                'label': _('Basic salary'),
                'quantity': None, 'rate': None, 'amount': wage,
            })
            rows.append({
                'label': _('÷ %(divisor).0f = plain hourly rate',
                           divisor=self.divisor or 0.0),
                'quantity': None, 'rate': hourly, 'amount': None,
            })
            rows.append({
                'label': _('× %(factor).2f overtime factor',
                           factor=self.factor or 1.0),
                'quantity': None,
                'rate': hourly * (self.factor or 1.0),
                'amount': None,
            })
            rows.append({
                'label': _('× %(qty).2f %(label)s', qty=quantity,
                           label=(self.qty_label or '').lower()),
                'quantity': quantity,
                'rate': hourly * (self.factor or 1.0),
                'amount': (wage / self.divisor * (self.factor or 1.0)
                           * quantity) if self.divisor else 0.0,
            })
            notes.append(_(
                'Computed in one expression so the hourly rate is never '
                'rounded before it is multiplied.'))
            return rows, notes

        if self.calculation == 'tiered':
            return self._detail_tiered(quantity, site=site,
                                       threshold=threshold)

        return rows, notes

    def _detail_tiered(self, quantity, site=None, threshold=0.0):
        """The tier waterfall, band by band — the driver's justification."""
        self.ensure_one()
        tiers = self._applicable_tiers(site)
        rows, notes = [], []

        rows.append({
            'label': _('Recorded %(label)s',
                       label=(self.qty_label or _('quantity')).lower()),
            'quantity': quantity or 0.0, 'rate': None, 'amount': None,
        })
        if threshold:
            rows.append({
                'label': _('Free allowance (required before earning)'),
                'quantity': -(threshold or 0.0), 'rate': None, 'amount': None,
            })
        remaining = max((quantity or 0.0) - (threshold or 0.0), 0.0)
        rows.append({
            'label': _('Earning quantity'),
            'quantity': remaining, 'rate': None, 'amount': None,
        })

        if not tiers:
            notes.append(_('No rate tiers apply, so nothing is earned.'))
            return rows, notes

        for index, tier in enumerate(tiers):
            if not remaining:
                break
            is_last = index == len(tiers) - 1
            take = remaining if (is_last or not tier.width) \
                else min(remaining, tier.width)
            rows.append({
                'label': tier.name or _('Tier %(n)s', n=index + 1),
                'quantity': take,
                'rate': tier.rate,
                'amount': take * (tier.rate or 0.0),
            })
            remaining -= take

        if site:
            site_rows = self.tier_ids.filtered(lambda t: t.site_id == site)
            notes.append(
                _('Using the rates set for %(site)s.', site=site.name)
                if site_rows else
                _('Using the default rates — %(site)s has none of its own.',
                  site=site.name))
        return rows, notes

    def _applicable_tiers(self, site=None):
        """Site-specific tiers when the site has any, otherwise the defaults."""
        self.ensure_one()
        site_tiers = self.tier_ids.filtered(lambda t: site and t.site_id == site)
        if site_tiers:
            return site_tiers.sorted('sequence')
        return self.tier_ids.filtered(lambda t: not t.site_id).sorted('sequence')

    def _resolve_tiered(self, quantity, site=None, threshold=0.0):
        """Waterfall ``quantity`` through the tiers above ``threshold``.

        ``threshold`` is the free allowance earned nothing is paid for — the
        driver's required trips for the days he actually worked. Tiers are
        consumed in order; whatever is left over falls into the last one.
        """
        self.ensure_one()
        tiers = self._applicable_tiers(site)
        if not tiers:
            return 0.0, 0.0

        remaining = max((quantity or 0.0) - (threshold or 0.0), 0.0)
        amount = 0.0
        for index, tier in enumerate(tiers):
            is_last = index == len(tiers) - 1
            if not remaining:
                break
            take = remaining if (is_last or not tier.width) \
                else min(remaining, tier.width)
            amount += take * (tier.rate or 0.0)
            remaining -= take
        earned_qty = max((quantity or 0.0) - (threshold or 0.0), 0.0)
        rate = (amount / earned_qty) if earned_qty else 0.0
        return rate, amount

    def _check_may_enter(self, user=None):
        """True when ``user`` is allowed to record this component."""
        self.ensure_one()
        user = user or self.env.user
        if self.env.su:
            return True
        if not self.entry_group_ids:
            return True
        return bool(self.entry_group_ids & user.all_group_ids)

    @api.model
    def _entered_by_current_user(self):
        """Components the current user may record — used to scope pickers."""
        return self.search([]).filtered(lambda c: c._check_may_enter())


class KswPayComponentOption(models.Model):
    """One choice inside a component — Breakfast, Lunch, Dinner.

    Meals were three components, so recording a month's meals meant three
    batches, three submissions and three lines on the register for what a
    supervisor thinks of as one thing. An option is the same pay at a
    different unit rate: one component, one screen, one column to pick from.

    Anything that is genuinely a *different* kind of pay — different scope,
    different calculation, a different person allowed to record it — stays a
    component of its own. Options only ever vary the rate.
    """
    _name = 'ksw.pay.option'
    _description = 'KSW Pay Component Option'
    _order = 'component_id, sequence, id'

    component_id = fields.Many2one(
        'ksw.pay.component', required=True, ondelete='cascade', index=True,
    )
    name = fields.Char(required=True, translate=True)
    code = fields.Char(
        help='Stable identifier used in exports and reports.',
    )
    sequence = fields.Integer(default=10)
    rate = fields.Float(
        digits=(16, 4), required=True,
        help='What one unit of this choice is worth — 20.00 per lunch.',
    )
    active = fields.Boolean(default=True)

    _unique_code = models.Constraint(
        'UNIQUE(component_id, code)',
        'Two options of the same component cannot share a code.')

    @api.constrains('rate')
    def _check_rate(self):
        for rec in self:
            if rec.rate < 0:
                raise ValidationError(_("An option's rate cannot be negative."))


class KswPayRateTier(models.Model):
    """One band of a tiered rate, optionally specific to a site."""
    _name = 'ksw.pay.rate.tier'
    _description = 'KSW Pay Rate Tier'
    _order = 'component_id, sequence, id'

    component_id = fields.Many2one(
        'ksw.pay.component', required=True, ondelete='cascade', index=True,
    )
    # Kept for the shape of the waterfall, not for choosing: with trips
    # recorded per department there is no site on the entry to match a
    # site-specific band against, so every live tier is a site-less one.
    site_id = fields.Many2one(
        'ksw.site', string='Work Site',
        domain="[('site_type', '=', 'calculation')]",
        help='Leave empty — the ladder applies to every location. Driver '
             'Trips is recorded per department, so a site-specific band '
             'has nothing to match on.',
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(help='Optional label, e.g. "Tier 2".')
    width = fields.Float(
        string='Band Size', digits=(16, 2),
        help='How much quantity this band covers. Leave at 0 on the last '
             'tier so it absorbs everything above the previous bands.',
    )
    rate = fields.Float(digits=(16, 4), required=True)

    @api.depends('name', 'component_id', 'site_id', 'rate')
    def _compute_display_name(self):
        for rec in self:
            label = rec.name or _('Tier')
            if rec.site_id:
                label = '%s — %s' % (label, rec.site_id.name)
            rec.display_name = '%s @ %.2f' % (label, rec.rate or 0.0)

    @api.constrains('width', 'rate')
    def _check_values(self):
        for rec in self:
            if rec.width < 0:
                raise ValidationError(_("A band size cannot be negative."))
            if rec.rate < 0:
                raise ValidationError(_("A tier rate cannot be negative."))
