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

SCOPE = [
    ('department', 'Department'),
    ('site', 'Work Site'),
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

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
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
    def _resolve(self, employee, quantity=0.0, site=None, threshold=0.0):
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
            return self.rate, (self.rate or 0.0) * quantity

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
                        threshold=0.0):
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
            rows.append({
                'label': _('%(label)s at the configured rate',
                           label=self.qty_label or _('Quantity')),
                'quantity': quantity,
                'rate': self.rate,
                'amount': (self.rate or 0.0) * quantity,
            })
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


class KswPayRateTier(models.Model):
    """One band of a tiered rate, optionally specific to a site."""
    _name = 'ksw.pay.rate.tier'
    _description = 'KSW Pay Rate Tier'
    _order = 'component_id, sequence, id'

    component_id = fields.Many2one(
        'ksw.pay.component', required=True, ondelete='cascade', index=True,
    )
    site_id = fields.Many2one(
        'ksw.site', string='Work Site',
        help='Leave empty to apply to every site.',
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
