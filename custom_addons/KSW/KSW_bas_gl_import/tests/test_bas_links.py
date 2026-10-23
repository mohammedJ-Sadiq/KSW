from datetime import date, datetime
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBasLinks(TransactionCase):
    """Delivery notes know their invoice; bills are bills; receipts pay invoices."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Account = cls.env['account.account']
        cls.company = cls.env.company

        def account(code, name, kind, reconcile=False):
            return Account.search([('code', '=', code)], limit=1) or Account.create({
                'code': code, 'name': name, 'account_type': kind, 'reconcile': reconcile})

        cls.client_acc = Account.create({
            'code': '129996001', 'name': 'KSW link client', 'x_bas_code': '129996001',
            'account_type': 'asset_receivable', 'reconcile': True})
        cls.vendor_acc = Account.create({
            'code': '2101999001', 'name': 'KSW link vendor', 'x_bas_code': '2101999001',
            'account_type': 'liability_payable', 'reconcile': True})
        cls.expense = Account.create({
            'code': '3101999002', 'name': 'KSW link expense', 'x_bas_code': '3101999002',
            'account_type': 'expense'})
        cls.bank = Account.create({
            'code': '1202999003', 'name': 'KSW link bank', 'x_bas_code': '1202999003',
            'account_type': 'asset_cash'})
        cls.income = account('4101999004', 'KSW link income', 'income')
        cls.vat_in = account('1209010001', 'Input VAT', 'asset_current')
        cls.client = cls.env['res.partner'].create({
            'name': 'KSW link client', 'x_bas_code': '129996001',
            'property_account_receivable_id': cls.client_acc.id})
        cls.vendor = cls.env['res.partner'].create({
            'name': 'KSW link vendor', 'x_bas_code': '2101999001', 'supplier_rank': 1})
        Tax = cls.env['account.tax']
        cls.t15 = Tax.create({
            'name': 'KSW 15% in', 'amount': 15.0, 'type_tax_use': 'purchase',
            'invoice_repartition_line_ids': [
                (0, 0, {'repartition_type': 'base'}),
                (0, 0, {'repartition_type': 'tax', 'account_id': cls.vat_in.id})],
            'refund_repartition_line_ids': [
                (0, 0, {'repartition_type': 'base'}),
                (0, 0, {'repartition_type': 'tax', 'account_id': cls.vat_in.id})],
        })
        cls.t0 = Tax.create({'name': 'KSW 0% in', 'amount': 0.0, 'type_tax_use': 'purchase'})
        cls.journal = cls.env['account.journal'].search([
            ('type', '=', 'general'), ('company_id', '=', cls.company.id)], limit=1)
        cls.G = cls.env['ksw.bas.gl.import']

    def _entry(self, key, day, debit_acc, credit_acc, amount):
        move = self.env['account.move'].create({
            'journal_id': self.journal.id, 'date': day, 'x_bas_key': key,
            'line_ids': [(0, 0, {'account_id': debit_acc.id, 'debit': amount}),
                         (0, 0, {'account_id': credit_acc.id, 'credit': amount})]})
        move.action_post()
        return move

    def _client_line(self, move):
        return move.line_ids.filtered(lambda l: l.account_id == self.client_acc)

    # -- delivery notes ---------------------------------------------------
    def test_a_delivery_note_knows_its_invoice(self):
        invoice = self._entry('002/3/110/99901', date(2026, 7, 31),
                              self.client_acc, self.income, 625.6)
        ptype = self.env['stock.picking.type'].search(
            [('code', '=', 'outgoing'), ('company_id', '=', self.company.id)], limit=1)
        note = self.env['stock.picking'].create({
            'picking_type_id': ptype.id, 'x_bas_key': '600/3/120/99902'})
        res = self.G._link_deliveries({'600/3/120/99902': '002/3/110/99901',
                                       '600/3/120/99903': '002/3/110/99901'})
        self.assertEqual(note.x_bas_invoice_id, invoice)
        self.assertEqual(invoice.x_bas_delivery_count, 1)
        self.assertEqual(res['linked'], 1)
        self.assertEqual(res['notes_not_in_odoo'], 1)

    # -- vendor bills -----------------------------------------------------
    def _rows(self, *lines):
        return [{'FCODE': f, 'TCODE': t, 'AMOUNT': a, 'REF': 'tyres ف7721',
                 'FDATE': datetime(2026, 8, 3)} for f, t, a in lines]

    def _accounts(self):
        return {a.x_bas_code or a.code: a for a in
                self.client_acc | self.vendor_acc | self.expense | self.bank | self.vat_in}

    def test_a_supplier_credit_becomes_a_bill_with_the_same_vat(self):
        rows = self._rows(('3101999002', '', 46920.0), ('1209010001', '', 7038.0),
                          ('', '2101999001', 53958.0))
        vals, why = self.G._build_bill(
            '006/0/115/99904', rows, self._accounts(), {'2101999001': self.vendor},
            self.t15, self.t0, self.G._bill_journal(self.company), self.company)
        self.assertIsNone(why)
        bill = self.env['account.move'].create(vals)
        bill.action_post()
        self.assertEqual(bill.move_type, 'in_invoice')
        self.assertAlmostEqual(bill.amount_total, 53958.0)
        self.assertAlmostEqual(bill.amount_tax, 7038.0)
        payable = bill.line_ids.filtered(lambda l: l.account_id == self.vendor_acc)
        self.assertAlmostEqual(payable.credit, 53958.0)

    def test_a_payment_to_a_supplier_is_not_a_bill(self):
        rows = self._rows(('1202999003', '', 500.0), ('', '2101999001', 500.0))
        vals, why = self.G._build_bill(
            '018/0/010/99905', rows, self._accounts(), {'2101999001': self.vendor},
            self.t15, self.t0, self.G._bill_journal(self.company), self.company)
        self.assertIsNone(vals)
        self.assertIn('not a bill', why)

    def test_vat_that_is_not_fifteen_percent_stays_an_entry(self):
        rows = self._rows(('3101999002', '', 1000.0), ('1209010001', '', 100.0),
                          ('', '2101999001', 1100.0))
        vals, why = self.G._build_bill(
            '018/0/010/99906', rows, self._accounts(), {'2101999001': self.vendor},
            self.t15, self.t0, self.G._bill_journal(self.company), self.company)
        self.assertIsNone(vals)
        self.assertIn('VAT', why)

    # -- receipts ---------------------------------------------------------
    def _link(self, names=None, fifo=True):
        with patch.object(type(self.G), '_named_invoice_numbers',
                          lambda self_, date_to: names or {}):
            return self.G.action_link_partner_ledgers(
                account_types=('asset_receivable',), account_codes=['129996001'], fifo=fifo)

    def test_a_receipt_pays_the_invoice_it_names(self):
        jan = self._entry('002/3/110/99939', date(2026, 1, 31), self.client_acc, self.income, 425.0)
        feb = self._entry('002/3/110/99940', date(2026, 2, 28), self.client_acc, self.income, 425.0)
        pay = self._entry('015/0/010/99941', date(2026, 3, 5), self.bank, self.client_acc, 425.0)
        self._link({('015/0/010/99941', '129996001'): {'99940'}})
        self.assertTrue(self._client_line(feb).reconciled)
        self.assertFalse(self._client_line(jan).reconciled)
        self.assertTrue(self._client_line(pay).reconciled)
        self.assertEqual(self._client_line(pay).partner_id, self.client)

    def test_an_unnamed_receipt_pays_the_oldest_invoice_first(self):
        jan = self._entry('002/3/110/30001', date(2026, 1, 31), self.client_acc, self.income, 100.0)
        feb = self._entry('002/3/110/30002', date(2026, 2, 28), self.client_acc, self.income, 200.0)
        self._entry('015/0/010/30003', date(2026, 3, 5), self.bank, self.client_acc, 150.0)
        self._link()
        self.assertTrue(self._client_line(jan).reconciled)
        self.assertAlmostEqual(self._client_line(feb).amount_residual, 150.0)

    def test_an_exact_amount_wins_over_oldest_first(self):
        jan = self._entry('002/3/110/31001', date(2026, 1, 31), self.client_acc, self.income, 100.0)
        feb = self._entry('002/3/110/31002', date(2026, 2, 28), self.client_acc, self.income, 200.0)
        self._entry('015/0/010/31003', date(2026, 3, 5), self.bank, self.client_acc, 200.0)
        self._link()
        self.assertTrue(self._client_line(feb).reconciled)
        self.assertAlmostEqual(self._client_line(jan).amount_residual, 100.0)

    def test_nothing_after_the_cut_off_is_matched(self):
        jan = self._entry('002/3/110/32001', date(2026, 1, 31), self.client_acc, self.income, 100.0)
        late = self._entry('015/0/010/32002', date(2026, 9, 5), self.bank, self.client_acc, 100.0)
        with patch.object(type(self.G), '_named_invoice_numbers', lambda s, d: {}):
            self.G.action_link_partner_ledgers(
                date_to=date(2026, 8, 31), account_types=('asset_receivable',),
                account_codes=['129996001'])
        self.assertFalse(self._client_line(jan).reconciled)
        self.assertFalse(self._client_line(late).partner_id)


@tagged('post_install', '-at_install')
class TestInvoiceFromNotes(TransactionCase):
    """A BAS monthly invoice shows its delivery notes, like one issued in Odoo."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Account = cls.env['account.account']
        cls.company = cls.env.company
        cls.client_acc = Account.create({
            'code': '129995001', 'name': 'KSW notes client', 'x_bas_code': '129995001',
            'account_type': 'asset_receivable', 'reconcile': True})
        cls.income = Account.create({
            'code': '4101995002', 'name': 'KSW notes income', 'x_bas_code': '4101995002',
            'account_type': 'income'})
        cls.bank = Account.create({
            'code': '1202995003', 'name': 'KSW notes bank', 'account_type': 'asset_cash'})
        cls.client = cls.env['res.partner'].create({
            'name': 'KSW notes client', 'x_bas_code': '129995001',
            'property_account_receivable_id': cls.client_acc.id})
        cls.tax = cls.env['account.tax'].create({
            'name': 'KSW 15% out', 'amount': 15.0, 'type_tax_use': 'sale'})
        cls.water = cls.env['product.product'].create({
            'name': 'KSW Sweet Water', 'x_bas_code': '99995'})
        cls.journal = cls.env['account.journal'].search([
            ('type', '=', 'sale'), ('company_id', '=', cls.company.id)], limit=1)
        cls.invoice = cls.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': cls.client.id,
            'journal_id': cls.journal.id, 'invoice_date': date(2026, 7, 31),
            'x_bas_key': '002/3/110/99950',
            'invoice_line_ids': [(0, 0, {'name': 'مبيعات', 'quantity': 1, 'price_unit': 1088.0,
                                         'account_id': cls.income.id,
                                         'tax_ids': [(6, 0, cls.tax.ids)]})]})
        cls.invoice.action_post()
        ptype = cls.env['stock.picking.type'].search(
            [('code', '=', 'outgoing'), ('company_id', '=', cls.company.id)], limit=1)
        for n in ('99951', '99952'):
            cls.env['stock.picking'].create({
                'picking_type_id': ptype.id, 'x_bas_key': f'600/3/120/{n}',
                'x_bas_invoice_id': cls.invoice.id})
        cls.G = cls.env['ksw.bas.gl.import']

    def _rows(self, *amounts):
        return [{'FTYPE2': 3, 'CODE2': '120', 'N': 99951 + i, 'ICODE': '99995',
                 'IDSCR': 'water', 'QUAN': 32.0, 'AMOUNT': a,
                 'FDATE': datetime(2026, 7, 10 + i)} for i, a in enumerate(amounts)]

    def _build(self, rows):
        revenue = {f"600/3/120/{r['N']}": '4101995002' for r in rows}
        return self.G._rebuilt_lines(
            self.invoice, rows, revenue, {'99995': self.water},
            {'4101995002': self.income}, 'x_water_summary' in self.invoice._fields)

    def test_the_invoice_gets_a_line_per_note_and_keeps_its_payment(self):
        pay = self.env['account.move'].create({
            'journal_id': self.env['account.journal'].search([
                ('type', '=', 'general'), ('company_id', '=', self.company.id)], limit=1).id,
            'date': date(2026, 8, 10),
            'line_ids': [(0, 0, {'account_id': self.bank.id, 'debit': 1251.2}),
                         (0, 0, {'account_id': self.client_acc.id, 'credit': 1251.2,
                                 'partner_id': self.client.id})]})
        pay.action_post()
        rec = lambda m: m.line_ids.filtered(lambda l: l.account_id == self.client_acc)
        (rec(self.invoice) | rec(pay)).reconcile()
        vals, why = self._build(self._rows(544.0, 544.0))
        self.assertIsNone(why)
        self.G._replace_invoice_lines(self.invoice, vals)
        products = self.invoice.invoice_line_ids.filtered(lambda l: l.display_type == 'product')
        self.assertEqual(len(products), 2)
        self.assertEqual(products.product_id, self.water)
        self.assertEqual(len(self.invoice.invoice_line_ids.filtered(
            lambda l: l.display_type == 'line_section')), 1)
        self.assertAlmostEqual(self.invoice.amount_total, 1251.2)
        self.assertEqual(self.invoice.state, 'posted')
        self.assertEqual(self.invoice.payment_state, 'paid')

    def test_notes_that_do_not_add_up_leave_the_invoice_alone(self):
        vals, why = self._build(self._rows(544.0, 500.0))
        self.assertIsNone(vals)
        self.assertIn('do not add up', why)
