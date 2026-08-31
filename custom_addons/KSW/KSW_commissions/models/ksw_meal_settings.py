"""KSW Commissions general settings — meal prices and the overtime rate.

The three meal prices are the rates of the **Meals** component's options
(Breakfast, Lunch, Dinner). They are shown here because this is where
people look for a price, but the option is the single place the figure
lives — a pay entry resolves its rate from it, so a second copy in
``ir.config_parameter`` would be a number that agrees with the payslip
only by luck. Editing either screen edits the same record.

The overtime divisor and factor are still plain parameters: they are read
by the legacy overtime helpers, not by a component.
"""
from odoo import api, fields, models


# The Meals component's options, in the order they are shown.
MEAL_OPTIONS = (
    ('ksw_meal_breakfast_price', 'KSW_commissions.pay_option_meal_breakfast'),
    ('ksw_meal_lunch_price', 'KSW_commissions.pay_option_meal_lunch'),
    ('ksw_meal_dinner_price', 'KSW_commissions.pay_option_meal_dinner'),
)

DEFAULT_BREAKFAST = 10.0
DEFAULT_LUNCH = 20.0
DEFAULT_DINNER = 15.0

# Overtime: basic salary ÷ divisor × factor = the hourly overtime rate.
PARAM_OT_DIVISOR = 'KSW_commissions.overtime_divisor'
PARAM_OT_FACTOR = 'KSW_commissions.overtime_factor'

DEFAULT_OT_DIVISOR = 240.0
DEFAULT_OT_FACTOR = 1.5


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    ksw_meal_breakfast_price = fields.Float(
        string='Breakfast Price', readonly=False,
        compute='_compute_meal_prices', inverse='_inverse_meal_prices',
        help='What one breakfast is worth (SAR). The same figure as the '
             'Breakfast option on the Meals pay component.',
    )
    ksw_meal_lunch_price = fields.Float(
        string='Lunch Price', readonly=False,
        compute='_compute_meal_prices', inverse='_inverse_meal_prices',
        help='What one lunch is worth (SAR). The same figure as the Lunch '
             'option on the Meals pay component.',
    )
    ksw_meal_dinner_price = fields.Float(
        string='Dinner Price', readonly=False,
        compute='_compute_meal_prices', inverse='_inverse_meal_prices',
        help='What one dinner is worth (SAR). The same figure as the Dinner '
             'option on the Meals pay component.',
    )

    def _meal_option(self, xmlid):
        """The option record behind one of the three prices, if it exists."""
        return self.env.ref(xmlid, raise_if_not_found=False)

    def _compute_meal_prices(self):
        for rec in self:
            for field, xmlid in MEAL_OPTIONS:
                option = rec._meal_option(xmlid)
                rec[field] = option.rate if option else 0.0

    def _inverse_meal_prices(self):
        for rec in self:
            for field, xmlid in MEAL_OPTIONS:
                option = rec._meal_option(xmlid)
                if option and option.rate != rec[field]:
                    option.sudo().write({'rate': rec[field]})

    ksw_overtime_divisor = fields.Float(
        string='Overtime Hours Divisor',
        config_parameter=PARAM_OT_DIVISOR,
        default=DEFAULT_OT_DIVISOR,
        help='Monthly hours the basic salary is divided by to get the '
             'plain hourly rate. KSW uses 240.',
    )
    ksw_overtime_factor = fields.Float(
        string='Overtime Factor',
        config_parameter=PARAM_OT_FACTOR,
        default=DEFAULT_OT_FACTOR,
        help='Multiplier applied to the hourly rate for overtime. Saudi '
             'Labour Law art. 107 sets this at 1.5.',
    )

    # ------------------------------------------------------------------
    # Helpers (used by ksw.location.allowance.line computes)
    # ------------------------------------------------------------------
    def _ksw_read_param(self, key, default):
        """Read a float ``ir.config_parameter``, falling back on default."""
        raw = self.env['ir.config_parameter'].sudo().get_param(key)
        if raw in (False, None, ''):
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    @api.model
    def _get_overtime_params(self):
        """Return ``(divisor, factor)`` for the overtime hourly rate.

        A zero or missing divisor falls back to the default rather than
        returning 0 — the caller would otherwise have to guard against a
        division by zero on every line.
        """
        divisor = self._ksw_read_param(PARAM_OT_DIVISOR, DEFAULT_OT_DIVISOR)
        factor = self._ksw_read_param(PARAM_OT_FACTOR, DEFAULT_OT_FACTOR)
        return (divisor or DEFAULT_OT_DIVISOR, factor)

    @api.model
    def _get_meal_prices(self):
        """Return ``(breakfast, lunch, dinner)`` — the option rates.

        Falls back to the seeded defaults only when an option is missing,
        which can only happen on a database where the Meals component was
        deleted by hand.
        """
        defaults = (DEFAULT_BREAKFAST, DEFAULT_LUNCH, DEFAULT_DINNER)
        prices = []
        for (_field, xmlid), default in zip(MEAL_OPTIONS, defaults):
            option = self.env.ref(xmlid, raise_if_not_found=False)
            prices.append(option.rate if option else default)
        return tuple(prices)

