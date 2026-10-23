from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBasSupplierPayment(TransactionCase):

    def test_mirror_payment_links_both_ways_and_never_posts(self):
        Account = self.env['account.account']
        payable = Account.create({'name': 'Supplier ZZ', 'code': '2101999911', 'reconcile': True,
                                  'account_type': 'liability_payable', 'x_bas_code': '2101999911'})
        expense = Account.create({'name': 'ZZ exp', 'code': '3999990031', 'account_type': 'expense'})
        bank_journal = self.env['account.journal'].create({'name': 'ZZ Bank', 'code': 'ZZB', 'type': 'bank'})
        vendor = self.env['res.partner'].create({'name': 'Vendor ZZ', 'x_bas_code': '2101999911'})
        bill = self.env['account.move'].create({
            'move_type': 'in_invoice', 'partner_id': vendor.id, 'invoice_date': '2026-11-02',
            'invoice_line_ids': [(0, 0, {'name': 'x', 'quantity': 1, 'price_unit': 500,
                                         'account_id': expense.id, 'tax_ids': [(6, 0, [])]})]})
        bill.line_ids.filtered(lambda l: l.display_type == 'payment_term').account_id = payable
        bill.action_post()
        # The BAS voucher: pays the supplier 300 (no partner on the line) and an advance, from the bank.
        advance = Account.create({'name': 'ZZ advance', 'code': '1207999911', 'account_type': 'asset_current'})
        voucher = self.env['account.move'].create({'move_type': 'entry', 'date': '2026-11-10', 'line_ids': [
            (0, 0, {'account_id': payable.id, 'debit': 300.0, 'name': 'دفعة'}),
            (0, 0, {'account_id': advance.id, 'debit': 50.0}),
            (0, 0, {'account_id': bank_journal.default_account_id.id, 'credit': 350.0})]})
        voucher.action_post()
        supplier_line = voucher.line_ids.filtered(lambda l: l.account_id == payable)
        supplier_line.partner_id = vendor
        (supplier_line | bill.line_ids.filtered(lambda l: l.account_id == payable)).reconcile()
        moves_before = self.env['account.move'].search_count([])

        res = self.env['ksw.bas.gl.import'].action_mirror_supplier_payments('2026-11-01', '2026-11-30')
        pay = self.env['account.payment'].search([('x_bas_line_id', '=', supplier_line.id)])
        self.assertEqual(res['created'], 1)
        self.assertEqual((pay.partner_id, pay.amount, pay.journal_id, str(pay.date)),
                         (vendor, 300.0, bank_journal, '2026-11-10'))
        self.assertFalse(pay.move_id, 'never an entry of its own')
        self.assertEqual(self.env['account.move'].search_count([]), moves_before)
        self.assertEqual(pay.state, 'paid', 'the 300 is fully used, even though the bill is only partly paid')
        self.assertEqual(pay.x_bas_bill_ids, bill)
        self.assertEqual(bill.x_bas_payment_ids, pay)
        self.assertEqual(voucher.x_bas_payment_ids, pay)
        self.assertEqual(bill.payment_state, 'partial', "the bill's status stays the ledger's")
        with self.assertRaises(UserError):
            pay.amount = 999
        pay.action_validate()
        self.assertFalse(pay.move_id)
        res = self.env['ksw.bas.gl.import'].action_mirror_supplier_payments('2026-11-01', '2026-11-30')
        self.assertEqual(res.get('created', 0), 0, 're-run creates nothing')
