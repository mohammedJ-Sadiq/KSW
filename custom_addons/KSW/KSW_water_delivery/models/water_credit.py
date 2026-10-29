"""Credit limit on water deliveries (Oct 2026).

Every water note confirms a sales order, and sale_financial_risk (OCA) answers
an over-limit confirmation with its "risk exceeded" dialog instead of
confirming, which left the note with no delivery and a meaningless error. The
water paths now ask the same question themselves, BEFORE anything is made,
and then confirm with `bypass_risk`:

* where someone can still act (the office form, Dispatch, Sell and Deliver
  Now): refused, in words; a Financial Risk Manager may go ahead, noted on
  the delivery;
* a delivery the phone already made (the capture queue): the water is in the
  customer's tank, so it is issued and HELD for a supervisor, never refused;
* cash sales: paid at the desk, so never checked.

The rule is sale_financial_risk's own (`sale.order.evaluate_risk_message`):
over risk already, over the sales-orders limit, or the total risk plus this
delivery over the credit limit. A no-op without that module.
"""
from odoo import _, api, models
from odoo.exceptions import UserError

RISK_MANAGER = 'account_financial_risk.group_account_financial_risk_manager'


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    @api.model
    def _water_credit_problem(self, partner, product, quantity):
        """Why delivering `quantity` of `product` to `partner` breaks his credit
        limit, or None."""
        partner = partner.commercial_partner_id.sudo()
        if not partner or 'risk_exception' not in partner._fields:
            return None
        if self.env.company.sudo().allow_overrisk_sale_confirmation \
                if 'allow_overrisk_sale_confirmation' in self.env.company._fields else False:
            return None
        rate = self.env['ksw.water.pricing']._rate_for(partner, product, quantity or 1.0)
        amount = 0.0
        if rate and quantity:
            taxes = product.sudo().taxes_id.filtered(
                lambda t: t.company_id == self.env.company)
            amount = taxes.compute_all(rate.fixed_price, self.env.company.currency_id,
                                       quantity, product=product, partner=partner)['total_included']
        over = (
            partner.risk_exception
            or (partner.risk_sale_order_limit
                and partner.risk_sale_order + amount > partner.risk_sale_order_limit)
            or (partner.risk_sale_order_include and partner.credit_limit
                and partner.risk_total + amount > partner.credit_limit)
        )
        if not over:
            return None
        return _('%s is over the credit limit.', partner.display_name)

    @api.model
    def _water_credit_override_allowed(self):
        return self.env['res.users'].sudo().browse(self.env.uid).has_group(RISK_MANAGER) \
            if self.env['ir.model.data']._xmlid_to_res_id(RISK_MANAGER, raise_if_not_found=False) \
            else False

    def _issue_water_note(self, payload, location_mode='raise'):
        partner = self.env['res.partner'].browse(payload['partner_id'])
        product = self.env['product.product'].browse(payload['product_id'])
        vehicle = self.env['ksw.fleet.vehicle'].browse(payload.get('vehicle_id') or [])
        quantity = self._water_quantity_in_product_uom(
            vehicle, payload.get('entered_qty') or 0.0, payload.get('entered_uom') or 'product')
        problem = self._water_credit_problem(partner, product, quantity)
        overridden = False
        if problem:
            if location_mode == 'raise':
                if not self._water_credit_override_allowed():
                    raise UserError(_('%s Call the office: a Financial Risk Manager can '
                                      'approve this delivery.', problem))
                overridden = True
            else:
                # Already delivered: issue it, held for a supervisor.
                payload = dict(payload, hold_reasons=[
                    *(payload.get('hold_reasons') or []),
                    _('%s Held for a supervisor.', problem)])
        picking = super(StockPicking, self.with_context(bypass_risk=True))._issue_water_note(
            payload, location_mode=location_mode)
        if overridden:
            picking.sudo().message_post(body=_(
                'Issued over the credit limit, approved by %s.', self.env.user.name))
        return picking


class KswWaterDispatch(models.Model):
    _inherit = 'ksw.water.dispatch'

    def _water_check_credit(self):
        """Refuse a dispatch to a customer over his limit, before anything is
        made. Cash sales are paid at the desk and never checked."""
        Picking = self.env['stock.picking']
        for dispatch in self.filtered(lambda d: not d.x_is_cash):
            problem = Picking._water_credit_problem(
                dispatch.partner_id, dispatch.product_id, dispatch.quantity_m3)
            if problem and not Picking._water_credit_override_allowed():
                raise UserError(_('%s A Financial Risk Manager can approve this '
                                  'delivery.', problem))

    def _water_dispatch_delivery(self):
        self._water_check_credit()
        return super(KswWaterDispatch, self.with_context(bypass_risk=True))._water_dispatch_delivery()

    def _water_sell_pickup(self):
        return super(KswWaterDispatch, self.with_context(bypass_risk=True))._water_sell_pickup()
