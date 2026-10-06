from odoo.tests import Form, TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPaymentJournal(TransactionCase):
    """A payment posts to the BAS bank the user picks, never a default one."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Account = cls.env['account.account']
        cls.bank_acc = Account.create({
            'name': 'KSW test bank', 'code': '1202999902', 'account_type': 'asset_cash'})
        revenue = Account.create({
            'name': 'KSW test revenue', 'code': '4101999902', 'account_type': 'income'})
        cls.env['account.journal']._ksw_ensure_bas_bank_journals()
        cls.bank_journal = cls.env['account.journal'].search([
            ('default_account_id', '=', cls.bank_acc.id), ('type', '=', 'bank')])
        cls.partner = cls.env['res.partner'].create({'name': 'KSW payment test client'})
        cls.invoice = cls.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': cls.partner.id,
            'invoice_line_ids': [(0, 0, {'name': 'Water', 'quantity': 1,
                                         'price_unit': 1000.0, 'tax_ids': False,
                                         'account_id': revenue.id})],
        })
        cls.invoice.action_post()

    def test_bank_journal_created_per_bas_account(self):
        self.assertEqual(len(self.bank_journal), 1)
        self.assertEqual(self.bank_journal.code, 'B9902')
        lines = (self.bank_journal.inbound_payment_method_line_ids
                 | self.bank_journal.outbound_payment_method_line_ids)
        self.assertEqual(lines.payment_account_id, self.bank_acc)
        # Re-running creates nothing new.
        self.env['account.journal']._ksw_ensure_bas_bank_journals()
        self.assertEqual(self.env['account.journal'].search_count(
            [('default_account_id', '=', self.bank_acc.id)]), 1)

    def test_template_bank_journal_retired(self):
        self.assertFalse(self.env['account.journal'].search([
            ('type', '=', 'bank'), ('company_id', '=', self.env.company.id),
            ('default_account_id.code', '=', '101001')]))

    def test_register_payment_asks_for_journal(self):
        ctx = {'active_model': 'account.move', 'active_ids': self.invoice.ids}
        form = Form(self.env['account.payment.register'].with_context(**ctx))
        self.assertFalse(form.journal_id, 'the journal must be chosen, not prefilled')
        form.journal_id = self.bank_journal
        self.assertEqual(form.amount, 1000.0)
        payment = form.save()._create_payments()
        bank_line = payment.move_id.line_ids.filtered(lambda l: l.debit)
        self.assertEqual(bank_line.account_id, self.bank_acc)
        self.assertEqual(self.invoice.payment_state, 'paid')

    def test_manual_payment_asks_for_journal(self):
        form = Form(self.env['account.payment'].with_context(
            default_payment_type='inbound', default_partner_type='customer'))
        form.partner_id = self.partner
        self.assertFalse(form.journal_id)

    def test_programmatic_payment_keeps_core_default(self):
        payment = self.env['account.payment'].create({
            'payment_type': 'inbound', 'partner_type': 'customer',
            'partner_id': self.partner.id, 'amount': 5.0})
        self.assertTrue(payment.journal_id)


@tagged('post_install', '-at_install')
class TestCustomerReceivable(TransactionCase):
    """A customer gets its own BAS leaf; nothing falls back to 102011."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env['ir.config_parameter'].sudo().set_param(
            'KSW_accounting_ux.customer_receivable_prefix', '129998')
        cls.env['account.account'].create({
            'name': 'KSW test existing client', 'code': '129998007',
            'account_type': 'asset_receivable', 'reconcile': True})
        cls.revenue = cls.env['account.account'].create({
            'name': 'KSW test revenue', 'code': '4101999903', 'account_type': 'income'})

    def test_new_customer_gets_next_leaf(self):
        partner = self.env['res.partner'].create({'name': 'عميل جديد', 'customer_rank': 1})
        acc = partner.property_account_receivable_id
        self.assertEqual(acc.code, '129998008')
        self.assertEqual(acc.name, 'عميل جديد')
        self.assertEqual(acc.account_type, 'asset_receivable')
        self.assertTrue(acc.reconcile)

    def test_contact_without_sales_gets_nothing(self):
        partner = self.env['res.partner'].create({'name': 'Just a contact'})
        self.assertNotEqual(partner.property_account_receivable_id.code or '', '129998008')
        self.assertFalse(self.env['account.account'].search([('code', '=', '129998008')]))

    def test_first_invoice_creates_leaf(self):
        partner = self.env['res.partner'].create({'name': 'Invoiced later'})
        partner.property_account_receivable_id = False
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': partner.id,
            'invoice_line_ids': [(0, 0, {'name': 'Water', 'quantity': 1, 'price_unit': 10.0,
                                         'tax_ids': False, 'account_id': self.revenue.id})],
        })
        move.action_post()
        recv = move.line_ids.filtered(lambda l: l.account_id.account_type == 'asset_receivable')
        self.assertEqual(recv.account_id.code, '129998008')
        self.assertEqual(partner.property_account_receivable_id, recv.account_id)

    def test_existing_leaf_kept(self):
        acc = self.env['account.account'].search([('code', '=', '129998007')])
        partner = self.env['res.partner'].create({
            'name': 'Has one', 'customer_rank': 1, 'property_account_receivable_id': acc.id})
        self.assertEqual(partner.property_account_receivable_id, acc)


@tagged('post_install', '-at_install')
class TestDefaultTax(TransactionCase):
    """A bill line with no product still gets the company's 15% VAT."""

    def test_bill_without_product_gets_company_purchase_tax(self):
        company = self.env.company
        expense = self.env['account.account'].create({
            'name': 'KSW test purchases', 'code': '3101999905', 'account_type': 'expense'})
        vendor = self.env['res.partner'].create({'name': 'KSW test vendor', 'supplier_rank': 1})
        with Form(self.env['account.move'].with_context(default_move_type='in_invoice')) as bill:
            bill.partner_id = vendor
            bill.invoice_date = '2030-03-31'
            with bill.invoice_line_ids.new() as line:
                line.name = 'ف338774'
                line.account_id = expense
                line.quantity = 40
                line.price_unit = 260
        bill = bill.save()
        expected = company.account_purchase_tax_id
        if bill.fiscal_position_id:
            expected = bill.fiscal_position_id.map_tax(expected)
        self.assertEqual(bill.invoice_line_ids.tax_ids, expected)
        self.assertAlmostEqual(bill.amount_tax, 1560.0)
        self.assertAlmostEqual(bill.amount_total, 11960.0)
