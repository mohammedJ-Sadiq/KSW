from datetime import date

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestClientLedger(TransactionCase):
    """BAS receipts get the client and settle the invoice of the same amount."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Account = cls.env['account.account']
        cls.recv = Account.create({
            'name': 'KSW test client', 'code': '129997001', 'x_bas_code': '129997001',
            'account_type': 'asset_receivable', 'reconcile': True})
        cls.bank = Account.create({'name': 'KSW test bank', 'code': '1202999903', 'account_type': 'asset_cash'})
        cls.income = Account.create({'name': 'KSW test income', 'code': '4101999904', 'account_type': 'income'})
        cls.partner = cls.env['res.partner'].create({
            'name': 'KSW ledger client', 'x_bas_code': '129997001',
            'property_account_receivable_id': cls.recv.id})
        cls.journal = cls.env['account.journal'].search([
            ('type', '=', 'general'), ('company_id', '=', cls.env.company.id)], limit=1)

    def _entry(self, day, debit_acc, credit_acc, amount):
        move = self.env['account.move'].create({
            'journal_id': self.journal.id, 'date': day, 'line_ids': [
                (0, 0, {'account_id': debit_acc.id, 'debit': amount}),
                (0, 0, {'account_id': credit_acc.id, 'credit': amount})]})
        move.action_post()
        return move.line_ids.filtered(lambda l: l.account_id == self.recv)

    def test_tags_and_matches_exact_amounts(self):
        jan = self._entry(date(2030, 1, 31), self.recv, self.income, 100.0)
        feb = self._entry(date(2030, 2, 28), self.recv, self.income, 250.0)
        paid_feb = self._entry(date(2030, 3, 10), self.bank, self.recv, 250.0)
        odd = self._entry(date(2030, 3, 11), self.bank, self.recv, 40.0)
        res = self.env['ksw.bas.gl.import'].action_link_client_ledger(account_codes=['129997001'])
        self.assertEqual(res['tagged'], 4)
        self.assertEqual((jan | feb | paid_feb | odd).partner_id, self.partner)
        self.assertTrue(feb.reconciled and paid_feb.reconciled)
        # No exact counterpart: left open for a person, never split by guesswork.
        self.assertFalse(jan.reconciled)
        self.assertFalse(odd.reconciled)

    def test_receipt_never_settles_a_later_invoice(self):
        receipt = self._entry(date(2030, 1, 5), self.bank, self.recv, 75.0)
        later = self._entry(date(2030, 1, 31), self.recv, self.income, 75.0)
        self.env['ksw.bas.gl.import'].action_link_client_ledger(account_codes=['129997001'])
        self.assertFalse(receipt.reconciled or later.reconciled)
