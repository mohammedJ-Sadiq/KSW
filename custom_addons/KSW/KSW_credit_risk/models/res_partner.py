from collections import defaultdict

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

# The fields that decide when this customer's invoices count as unpaid.
_GRACE_FIELDS = ('x_risk_grace_policy', 'x_risk_grace_days')


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # OCA account_financial_risk moves an invoice from "open" to "unpaid" once
    # it is past its due date + res.company.invoice_unpaid_margin: one number
    # for every customer. BAS keeps a different stop per customer (LIMT_DAYS,
    # 60 to 300 days against a 30/60/90 term), so the margin is resolved per
    # commercial partner here and fed to the OCA compute through the context.
    #
    # Empty policy = nobody has decided: the company margin applies, and the
    # BAS sync may take the customer over ('bas'). 'company' and 'customer'
    # are human choices the sync never touches (same rule as
    # x_payment_term_from_bas in KSW_ext_sync).
    x_risk_grace_policy = fields.Selection(
        [('company', 'Company Default'),
         ('bas', 'Follow BAS'),
         ('customer', 'This Customer')],
        string='Grace Period', tracking=True, copy=False,
        help='Which grace period decides when this customer\'s overdue '
             'invoices count as unpaid (and stop new sales orders):\n'
             '- Company Default: the Maturity Margin in Accounting settings.\n'
             '- Follow BAS: BAS credit-stop days minus its payment term '
             '(LIMT_DAYS - INVDAYS), refreshed by every BAS customer sync.\n'
             '- This Customer: the days below, set by hand; BAS never '
             'overwrites them.\n'
             'Empty behaves as Company Default until the BAS sync fills it.',
    )
    x_risk_grace_days = fields.Integer(
        string='Grace Days', tracking=True, copy=False,
        help='Days after an invoice\'s due date before it counts as unpaid. '
             'E.g. a 30-day payment term with 60 grace days stops the '
             'customer on day 91. Editing this by hand switches the '
             'customer to "This Customer".',
    )
    x_risk_effective_grace = fields.Integer(
        string='Grace Applied (Days)', compute='_compute_risk_effective_grace',
        help='The grace days actually used for this customer right now.',
    )

    @api.model
    def _commercial_fields(self):
        return super()._commercial_fields() + list(_GRACE_FIELDS)

    def _risk_grace_days(self):
        self.ensure_one()
        partner = self.commercial_partner_id or self
        if partner.x_risk_grace_policy in ('bas', 'customer'):
            return partner.x_risk_grace_days
        return self.env.company.invoice_unpaid_margin

    @api.depends('x_risk_grace_policy', 'x_risk_grace_days',
                 'commercial_partner_id.x_risk_grace_policy',
                 'commercial_partner_id.x_risk_grace_days')
    @api.depends_context('company')
    def _compute_risk_effective_grace(self):
        for partner in self:
            partner.x_risk_effective_grace = partner._risk_grace_days()

    @api.constrains('x_risk_grace_days')
    def _check_risk_grace_days(self):
        if any(p.x_risk_grace_days < 0 for p in self):
            raise ValidationError(_('Grace days cannot be negative.'))

    def write(self, vals):
        from_bas = self.env.context.get('ksw_bas_risk_grace')
        if ('x_risk_grace_days' in vals and 'x_risk_grace_policy' not in vals
                and not from_bas):
            vals = dict(vals, x_risk_grace_policy='customer')
        if (not from_bas and not self.env.su
                and any(f in vals for f in _GRACE_FIELDS)
                and not self.env.user.has_group(
                    'account_financial_risk.group_account_financial_risk_manager')):
            # Only a real change on the customer itself: contacts receive the
            # same values through the commercial-field sync whenever anyone
            # links them to a company, and that must not need the manager.
            changed = self.filtered(lambda p: (
                isinstance(p.id, int) and p.commercial_partner_id == p
                and any(p[f] != vals[f] for f in _GRACE_FIELDS if f in vals)))
            if changed:
                raise UserError(_(
                    'Only a Financial Risk Manager can change a customer\'s '
                    'grace period.'))
        return super().write(vals)

    # ------------------------------------------------------------------
    # Hand the per-customer margin to the OCA computation
    # ------------------------------------------------------------------
    @api.model
    def _max_risk_date_due(self):
        days = self.env.context.get('ksw_risk_grace_days')
        if days is None:
            return super()._max_risk_date_due()
        return fields.Date.to_string(
            fields.Date.context_today(self) - relativedelta(days=days))

    @api.depends('x_risk_grace_policy', 'x_risk_grace_days',
                 'commercial_partner_id.x_risk_grace_policy',
                 'commercial_partner_id.x_risk_grace_days')
    def _compute_risk_account_amount(self):
        # OCA computes all of self against one cut-off date; split self by
        # grace so each customer is measured against his own.
        by_grace = defaultdict(lambda: self.browse())
        for partner in self:
            by_grace[partner._risk_grace_days()] |= partner
        for days, partners in by_grace.items():
            super(ResPartner, partners.with_context(
                ksw_risk_grace_days=days))._compute_risk_account_amount()

    def _get_field_risk_model_domain(self, field_name):
        # The pivot behind each amount on the Financial Risk tab must list
        # the same invoices the amount was computed from.
        if self:
            self = self.with_context(
                ksw_risk_grace_days=self[:1]._risk_grace_days())
        return super()._get_field_risk_model_domain(field_name)
