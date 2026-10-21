from odoo import _, api, fields, models
from odoo.exceptions import UserError


class KswPaySettlementNewWizard(models.TransientModel):
    """New Settlement Voucher: show what came before, then ask.

    Every voucher, a draft included, holds its transactions: they are on no
    other voucher until it is posted or discarded. So before a new one is
    started the accountant sees the drafts still holding transactions and
    the last voucher posted, and confirms that the new one is to carry only
    what happened after them.
    """
    _name = 'ksw.pay.settlement.new.wizard'
    _description = 'New Commission Settlement Voucher'

    draft_ids = fields.Many2many(
        'ksw.pay.settlement', string='Drafts Holding Transactions',
        readonly=True)
    last_posted_id = fields.Many2one(
        'ksw.pay.settlement', string='Last Posted Voucher', readonly=True)
    last_posted_date = fields.Date(
        related='last_posted_id.date', string='Voucher Date')
    last_posted_number = fields.Char(
        related='last_posted_id.journal_number', string='BAS Journal Number')
    last_posted_by_id = fields.Many2one(
        related='last_posted_id.done_by_id', string='Posted By')
    last_posted_on = fields.Datetime(
        related='last_posted_id.done_date', string='Posted On')
    is_first = fields.Boolean(readonly=True)
    currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id)
    pending_count = fields.Integer(string='New Transactions', readonly=True)
    pending_right = fields.Monetary(string='Commission Right', readonly=True)
    pending_paid = fields.Monetary(string='Paid by Bank', readonly=True)
    pending_deducted = fields.Monetary(string='Deducted', readonly=True)
    confirmed = fields.Boolean(
        string='I have reviewed the vouchers above and want a new one '
               'carrying only what came after them')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        Settlement = self.env['ksw.pay.settlement']
        Settlement._check_accountant()
        drafts = Settlement.search([('state', '=', 'draft')])
        last = Settlement.search([('state', '=', 'done')],
                                 order='done_date desc, id desc', limit=1)
        pending = Settlement.browse()._pending_deltas()
        res.update({
            'draft_ids': [(6, 0, drafts.ids)],
            'last_posted_id': last.id,
            'is_first': not Settlement.search_count([]),
            'pending_count': len(pending),
            'pending_right': sum(v['amount'] for v in pending
                                 if v['kind'] == 'accrual'),
            'pending_paid': sum(v['amount'] for v in pending
                                if v['kind'] == 'payment'),
            'pending_deducted': sum(v['amount'] for v in pending
                                    if v['kind'] == 'deduction'),
        })
        return res

    def action_create(self):
        self.ensure_one()
        if (self.draft_ids or self.last_posted_id) and not self.confirmed:
            raise UserError(_(
                'Tick the confirmation first: there are vouchers before this '
                'one, and the new voucher carries only what they do not.'))
        if not self.pending_count:
            raise UserError(_(
                'There is nothing new to post: every payment and deduction '
                'is already on a voucher.'))
        voucher = self.env['ksw.pay.settlement'].with_context(
            ksw_settlement_new_confirmed=True).create({})
        return voucher._open_form()

    def action_open_draft(self):
        self.ensure_one()
        if len(self.draft_ids) == 1:
            return self.draft_ids._open_form()
        return self.env['ksw.pay.settlement']._open_history(
            domain=[('id', 'in', self.draft_ids.ids)])

    def action_open_history(self):
        return self.env['ksw.pay.settlement']._open_history()
