from odoo import api, models

# Context flag set only while the web client builds a payment screen.  The
# journal is the bank (or till) the money actually moved through; KSW banks
# with several, so it is the person recording the payment who knows which one.
# Prefilling "the first bank journal" posted every payment to a bank BAS does
# not have.  Programmatic callers (the BAS cash-sale import, tests) keep core's
# default because they never go through onchange().
ASK_JOURNAL = 'ksw_ask_payment_journal'


def _ensure_customer_receivable(env, vals_list, is_customer):
    """Give the partner its own receivable before the entry reads it."""
    partner_ids = {v['partner_id'] for v in vals_list if v.get('partner_id') and is_customer(v)}
    if partner_ids:
        env['res.partner'].browse(partner_ids)._ksw_ensure_receivable_account()


class AccountMove(models.Model):
    _inherit = 'account.move'

    @api.model_create_multi
    def create(self, vals_list):
        default_type = self.env.context.get('default_move_type')
        _ensure_customer_receivable(self.env, vals_list, lambda v: (
            v.get('move_type', default_type) in ('out_invoice', 'out_refund', 'out_receipt')))
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('partner_id'):
            _ensure_customer_receivable(self.env, [vals], lambda v: any(
                m.move_type in ('out_invoice', 'out_refund', 'out_receipt') for m in self))
        return super().write(vals)


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    @api.model_create_multi
    def create(self, vals_list):
        _ensure_customer_receivable(self.env, vals_list, lambda v: (
            v.get('partner_type', 'customer') == 'customer'))
        return super().create(vals_list)

    def onchange(self, values, field_names, fields_spec):
        return super(AccountPayment, self.with_context(**{ASK_JOURNAL: True})).onchange(
            values, field_names, fields_spec)

    @api.depends('company_id', 'partner_id')
    def _compute_journal_id(self):
        if not self.env.context.get(ASK_JOURNAL):
            return super()._compute_journal_id()
        for payment in self:
            company = payment.company_id or self.env.company
            if payment.journal_id.company_id != company:
                payment.journal_id = False


class AccountPaymentRegister(models.TransientModel):
    _inherit = 'account.payment.register'

    def onchange(self, values, field_names, fields_spec):
        return super(AccountPaymentRegister, self.with_context(**{ASK_JOURNAL: True})).onchange(
            values, field_names, fields_spec)

    @api.depends('available_journal_ids')
    def _compute_journal_id(self):
        if not self.env.context.get(ASK_JOURNAL):
            return super()._compute_journal_id()
        for wizard in self:
            if wizard.journal_id not in wizard.available_journal_ids:
                wizard.journal_id = False
