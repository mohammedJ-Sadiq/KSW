from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

DEFAULT_SITE_XMLID = 'KSW_commissions.site_trip_default'


class KswSite(models.Model):
    """A place work is paid for — and, once, the trip calculation too.

    The model was carrying two jobs that only looked alike:

    **Overtime location** (``site_type = 'location'``) — somewhere work
    happened that has to be named on an overtime or location-allowance
    entry (``ksw.pay.entry.location_id``). This is what a work site is to
    everyone who uses the app, and it is all but one of the records.

    **Trip calculation** (``site_type = 'calculation'``) — a single system
    record, seeded as **Default**, holding the driver-trip settings that
    apply to every location. Driver Trips is recorded per *department*
    now, not per site, so there is nothing left to vary per site and
    nothing to choose: this record is hidden from the Work Sites list and
    from every picker, and is reached through Configuration → Driver Trip
    Settings.

    Use :meth:`_trip_settings` rather than searching for it.
    """
    _name = 'ksw.site'
    _description = 'KSW Work Site'
    _order = 'site_type, name'

    name = fields.Char(required=True, translate=True)
    site_type = fields.Selection(
        [('location', 'Overtime Location'),
         ('calculation', 'Trip Calculation (system)')],
        string='Site Type', required=True, default='location', index=True,
        help='Overtime Location: a place named on an overtime or '
             'location-allowance entry. Trip Calculation: the single hidden '
             'record holding the driver-trip settings used for every '
             'location — not something to create a second of.',
    )
    code = fields.Char(
        help='Optional short code used in driver-commission filenames.',
    )
    active = fields.Boolean(default=True)

    # ------------------------------------------------------------------
    # Trip-tier configuration (Saudi driver commission scheme)
    # Read from the single 'calculation' record only. A location never
    # carries any of this — see _trip_settings.
    # ------------------------------------------------------------------
    # Tier 1: required trips for full attendance (no commission earned).
    # Tiers 2-4: fixed-band commission. Tier 5: open-ended at top rate.
    required_trips_full_month = fields.Integer(
        string='Required Trips (full month)', default=50,
        help='Required multiplied-trips for an employee who worked the '
             'full 30 days. Pro-rated for partial-month attendance: '
             'required_trips = round(required_trips_full_month * '
             'worked_days / 30).',
    )
    tier2_trips = fields.Integer(default=40)
    tier3_trips = fields.Integer(default=40)
    tier4_trips = fields.Integer(default=40)
    tier2_rate = fields.Float(default=10.0, digits=(8, 2))
    tier3_rate = fields.Float(default=15.0, digits=(8, 2))
    tier4_rate = fields.Float(default=20.0, digits=(8, 2))
    tier5_rate = fields.Float(default=25.0, digits=(8, 2))
    note = fields.Text()

    @api.model
    def _trip_settings(self):
        """The single record the driver-trip calculation reads.

        Resolved by xmlid, never by search: it is deliberately outside
        every domain in the app, so a search for it would be a search
        nobody else is allowed to make.
        """
        return self.env.ref(DEFAULT_SITE_XMLID, raise_if_not_found=False) \
            or self.browse()

    @api.constrains('site_type', 'active')
    def _check_single_calculation_site(self):
        """Exactly one calculation record, and it does not get archived.

        "The calculation for all locations" only means anything while
        there is one of it. A second would be invisible (both are hidden)
        and silently ignored, which is the worst way for configuration to
        be wrong.
        """
        for rec in self.filtered(lambda s: s.site_type == 'calculation'):
            if not rec.active:
                raise ValidationError(_(
                    'The driver trip settings record cannot be archived — '
                    'the trip calculation reads it.'))
            others = self.search_count([
                ('site_type', '=', 'calculation'), ('id', '!=', rec.id),
            ])
            if others:
                raise ValidationError(_(
                    'There is already a driver trip settings record. The '
                    'trip calculation is the same for every location, so '
                    'there is only ever one.'))
