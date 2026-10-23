"""BAS's supplier payments as Odoo vendor payments, without touching the ledger.

BAS pays suppliers inside large mixed vouchers (018): one voucher debits a
supplier's 2101 account next to commissions, employee advances, branch cash
and customer receipts, and credits one or more bank/cash accounts. The GL
import keeps each voucher as one journal entry, so **Vendors ▸ Payments** was
empty although 28 M SAR of supplier payments were in the books.

Each supplier line becomes one **mirror payment**: an ``account.payment`` with
the vendor, amount, date and the bank/cash journal the voucher paid from, that
**never has a journal entry of its own** (``x_bas_mirror``). The voucher stays
the accounting; nothing is posted twice and no figure moves. Rules that follow:

* ``_generate_journal_entry`` skips mirrors. Community Odoo gives every new
  payment an outstanding account and posts an entry the moment its state
  becomes in-process or paid; a mirror must never get one.
* Mirrors are not linked through ``invoice_ids``: in Community a bill linked
  to an entry-less payment reads *Paid* even with an amount still owed
  (``account.move._compute_payment_state``). The bills a mirror settled are
  read from the real matching of its BAS line instead (``x_bas_bill_ids``), so
  a bill's status stays the ledger's.
* Its accounting fields cannot be edited, and it is never matched to a bank
  statement: the voucher's bank line is what a statement matches.
"""
import logging
from collections import defaultdict

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)
_LOCKED = {'amount', 'partner_id', 'journal_id', 'date', 'payment_type', 'partner_type',
           'x_bas_line_id', 'currency_id', 'outstanding_account_id', 'move_id'}


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    x_bas_mirror = fields.Boolean(
        string='BAS Payment', readonly=True, copy=False, index=True,
        help='Recorded in BAS inside a payment voucher. The voucher is the accounting '
             'entry; this payment has none of its own.')
    x_bas_line_id = fields.Many2one('account.move.line', string='BAS Voucher Line', readonly=True,
                                    copy=False, index=True, ondelete='set null')
    x_bas_move_id = fields.Many2one('account.move', string='BAS Voucher', readonly=True, copy=False,
                                    index=True)
    x_bas_number = fields.Char(related='x_bas_move_id.x_bas_number', string='BAS Document No.')
    x_bas_bill_ids = fields.Many2many('account.move', string='Bills Settled',
                                      compute='_compute_bas_bills', compute_sudo=True)
    x_bas_bill_count = fields.Integer(compute='_compute_bas_bills', compute_sudo=True)

    _x_bas_line_uniq = models.Constraint('UNIQUE (x_bas_line_id)',
                                         'This BAS voucher line already has a payment.')

    def _compute_bas_bills(self):
        for pay in self:
            line = pay.x_bas_line_id
            bills = (line.matched_credit_ids.credit_move_id.move_id
                     | line.matched_debit_ids.debit_move_id.move_id) if line else self.env['account.move']
            bills = bills.filtered(lambda m: m.is_purchase_document(include_receipts=True))
            pay.x_bas_bill_ids = bills
            pay.x_bas_bill_count = len(bills)

    def _generate_journal_entry(self, write_off_line_vals=None, force_balance=None, line_ids=None):
        # A mirror's accounting is its BAS voucher: never a second entry.
        return super(AccountPayment, self.filtered(lambda p: not p.x_bas_mirror))._generate_journal_entry(
            write_off_line_vals=write_off_line_vals, force_balance=force_balance, line_ids=line_ids)

    def write(self, vals):
        if not self.env.context.get('ksw_bas_mirror_sync') and _LOCKED & set(vals) \
                and self.filtered('x_bas_mirror'):
            raise UserError(_(
                'This payment was made in BAS. Its amount, vendor, journal and date come from '
                'the BAS voucher %(voucher)s and cannot be changed here.',
                voucher=self.filtered('x_bas_mirror')[:1].x_bas_move_id.name))
        return super().write(vals)

    def action_view_bas_bills(self):
        self.ensure_one()
        return self.x_bas_bill_ids._get_records_action(name=_('Bills Settled'))

    def action_view_bas_voucher(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'account.move',
                'res_id': self.x_bas_move_id.id, 'view_mode': 'form', 'target': 'current'}


class AccountMove(models.Model):
    _inherit = 'account.move'

    # Both ways: the BAS payments that settled this bill, and on a BAS voucher
    # the payments recorded from it.
    x_bas_payment_ids = fields.Many2many('account.payment', string='BAS Payments',
                                         compute='_compute_bas_payments', compute_sudo=True)
    x_bas_payment_count = fields.Integer(compute='_compute_bas_payments', compute_sudo=True)

    def _compute_bas_payments(self):
        Payment = self.env['account.payment']
        for move in self:
            if move.is_purchase_document(include_receipts=True):
                lines = move.line_ids.filtered(lambda l: l.account_id.account_type == 'liability_payable')
                counter = lines.matched_debit_ids.debit_move_id | lines.matched_credit_ids.credit_move_id
                pays = Payment.search([('x_bas_line_id', 'in', counter.ids)]) if counter else Payment
            elif move.move_type == 'entry' and move.id:
                pays = Payment.search([('x_bas_move_id', '=', move.id)])
            else:
                pays = Payment
            move.x_bas_payment_ids = pays
            move.x_bas_payment_count = len(pays)

    def action_view_bas_payments(self):
        self.ensure_one()
        return self.x_bas_payment_ids._get_records_action(name=_('BAS Payments'))


class BasGlImport(models.Model):
    _inherit = 'ksw.bas.gl.import'

    @api.model
    def action_mirror_supplier_payments(self, date_from=None, date_to=None, company=None):
        """One mirror payment per supplier line of a BAS payment voucher. Re-run safe."""
        company = company or self.env.company
        MoveLine = self.env['account.move.line'].sudo()
        domain = [('parent_state', '=', 'posted'), ('move_id.move_type', '=', 'entry'),
                  ('company_id', '=', company.id), ('debit', '>', 0),
                  ('account_id.account_type', '=', 'liability_payable'),
                  ('account_id.code', '=like', '2101%')]
        if date_from:
            domain.append(('date', '>=', date_from))
        if date_to:
            domain.append(('date', '<=', date_to))
        lines = MoveLine.search(domain, order='date, id')
        done = set(self.env['account.payment'].sudo().search(
            [('x_bas_line_id', 'in', lines.ids)]).mapped('x_bas_line_id').ids)
        journals = {j.default_account_id.id: j for j in self.env['account.journal'].sudo().search(
            [('type', 'in', ('bank', 'cash')), ('company_id', '=', company.id)]) if j.default_account_id}
        vendors = {p.x_bas_code: p for p in self.env['res.partner'].sudo().search(
            [('x_bas_code', '=like', '2101%')])}
        Payment = self.env['account.payment'].sudo().with_context(
            ksw_bas_mirror_sync=True, tracking_disable=True)
        stats, skipped = defaultdict(int), defaultdict(int)
        for line in lines:
            if line.id in done:
                stats['already there'] += 1
                continue
            voucher = line.move_id
            paid_from = voucher.line_ids.filtered(lambda l: l.credit > 0 and l.account_id.id in journals)
            if not paid_from:
                skipped['no bank or cash line in the voucher (an offset, not a payment)'] += 1
                continue
            exact = paid_from.filtered(lambda l: voucher.currency_id.compare_amounts(l.credit, line.debit) == 0)
            source = exact[:1] or paid_from.sorted('credit', reverse=True)[:1]
            journal = journals[source.account_id.id]
            vendor = line.partner_id or vendors.get(line.account_id.x_bas_code)
            if not vendor:
                skipped['no vendor for the supplier account'] += 1
                continue
            method = journal.outbound_payment_method_line_ids[:1]
            pay = Payment.create({
                'payment_type': 'outbound', 'partner_type': 'supplier',
                'partner_id': vendor.id, 'amount': line.debit, 'date': line.date,
                'journal_id': journal.id, 'payment_method_line_id': method.id,
                'memo': ' · '.join(filter(None, [voucher.name, line.name])),
                'x_bas_mirror': True, 'x_bas_line_id': line.id, 'x_bas_move_id': voucher.id,
                'company_id': company.id,
            })
            # Community gave it an outstanding account at create; a mirror has none, so the
            # state below can never generate an entry (``_generate_journal_entry`` skips it too).
            pay.outstanding_account_id = False
            pay.state = 'paid' if line.reconciled or company.currency_id.is_zero(line.amount_residual) \
                else 'in_process'
            stats['created'] += 1
        res = {**stats, 'skipped': dict(skipped), 'lines': len(lines)}
        _logger.info('BAS supplier payments %s..%s: %s', date_from, date_to, res)
        return res
