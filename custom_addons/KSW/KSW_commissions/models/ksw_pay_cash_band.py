# -*- coding: utf-8 -*-
"""Why a cash load is weighted differently — the amount bands.

A cash sale is booked in BAS to the **cash box**, not to the customer. So
the account the line carries is «حساب الصندوق», whose own `cod10.FACTORE`
is the branch's rate (0.75, or 1.0 at the factory) — and every cash load
comes out weighted as though it went next door, however far it really went.
The destination is written in `vou10.REMARK` as free text and exists in no
customer master, so there is nothing to look it up in.

What the amount does record is the distance: a longer run costs more. So
the factor for a cash load is read off the line's **total amount**, in
bands. This model holds those bands, editable, because they are a
commercial decision and not a fact about the software.

The bands are **sharp at the edges by design**: a load at 450.00 is worth
1.0 and at 451.00 is worth 1.5. A discount that moves an invoice across a
boundary moves the driver's weighting with it. That is the rule as given.
"""
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

#: BAS accounts that are a till, not a customer. A line sitting on one of
#: these is a cash sale and its factor comes from the amount band.
#: Overridable via `ir.config_parameter` `ksw_commissions.bas_cash_accounts`.
CASH_ACCOUNTS_DEFAULT = ('1201020001,1201020003,1201020004,'
                         '1201020006,1201020007,1201020010')


class KswPayCashBand(models.Model):
    _name = 'ksw.pay.cash.band'
    _description = 'KSW Cash Load — Amount Band'
    _order = 'amount_from'

    name = fields.Char(compute='_compute_name', store=True)
    amount_from = fields.Float(
        string='From', required=True, digits=(16, 2),
        help='Inclusive. The line total, not the unit price.',
    )
    amount_to = fields.Float(
        string='To', digits=(16, 2),
        help='Inclusive. Leave at 0 for the open-ended top band.',
    )
    factor = fields.Float(
        string='Weighting', required=True, digits=(16, 4),
        help='What one load in this band counts as — the same scale as '
             '«الرد المضاعف» (0.75, 1, 1.5 …).',
    )
    active = fields.Boolean(default=True)

    @api.depends('amount_from', 'amount_to', 'factor')
    def _compute_name(self):
        for rec in self:
            if rec.amount_to:
                span = '%.0f – %.0f' % (rec.amount_from, rec.amount_to)
            else:
                span = '> %.0f' % rec.amount_from
            rec.name = '%s  →  %g' % (span, rec.factor)

    @api.constrains('amount_from', 'amount_to', 'factor')
    def _check_band(self):
        for rec in self:
            if rec.amount_to and rec.amount_to < rec.amount_from:
                raise ValidationError(_(
                    'Band "%(name)s" ends before it starts.', name=rec.name))
            if rec.factor < 0:
                raise ValidationError(_(
                    'A band cannot carry a negative weighting.'))
            overlap = self.search([
                ('id', '!=', rec.id),
                ('amount_from', '<=', rec.amount_to or 10 ** 9),
                '|', ('amount_to', '=', 0),
                ('amount_to', '>=', rec.amount_from),
            ], limit=1)
            if overlap:
                raise ValidationError(_(
                    'Band "%(a)s" overlaps "%(b)s". Two bands cannot claim '
                    'the same amount — the weighting would depend on which '
                    'one happened to be read first.',
                    a=rec.name, b=overlap.name))

    # ------------------------------------------------------------------
    @api.model
    def _cash_accounts(self):
        """The BAS accounts that are a till rather than a customer."""
        raw = self.env['ir.config_parameter'].sudo().get_param(
            'ksw_commissions.bas_cash_accounts', CASH_ACCOUNTS_DEFAULT)
        return tuple(a.strip() for a in (raw or '').split(',') if a.strip())

    @api.model
    def _sql_case(self, amount_expr):
        """A SQL CASE that turns a line amount into its band factor.

        Built from the band records rather than hard-coded, so editing a
        band in Configuration changes what the next import pays. An amount
        below every band earns **nothing**: it has not been measured, and
        guessing a floor would quietly pay for it.
        """
        bands = self.search([], order='amount_from desc')
        if not bands:
            return None
        parts = []
        for band in bands:
            # float() on both sides — these are our own numeric columns,
            # never user text, and this is interpolated into SQL.
            if band.amount_to:
                parts.append('WHEN %s BETWEEN %f AND %f THEN %f'
                             % (amount_expr, float(band.amount_from),
                                float(band.amount_to), float(band.factor)))
            else:
                parts.append('WHEN %s >= %f THEN %f'
                             % (amount_expr, float(band.amount_from),
                                float(band.factor)))
        return 'CASE %s ELSE 0 END' % ' '.join(parts)
