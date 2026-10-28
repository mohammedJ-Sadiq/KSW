from dateutil.relativedelta import relativedelta

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestCustomerGrace(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.invoice_unpaid_margin = 0
        cls.receivable = cls.env['account.account'].create({
            'name': 'KSW Test Receivable', 'code': 'KSWT1203',
            'account_type': 'asset_receivable', 'reconcile': True,
        })
        cls.income = cls.env['account.account'].create({
            'name': 'KSW Test Income', 'code': 'KSWT4101',
            'account_type': 'income',
        })
        cls.journal = cls.env['account.journal'].create({
            'name': 'KSW Test Misc', 'code': 'KSWTM', 'type': 'general',
        })
        cls.long_grace = cls._customer('Long Grace Customer')
        cls.short_grace = cls._customer('Short Grace Customer')
        manager_group = cls.env.ref(
            'account_financial_risk.group_account_financial_risk_manager')
        cls.risk_manager = cls.env['res.users'].create({
            'name': 'Risk Manager', 'login': 'ksw_risk_manager',
            'group_ids': [Command.set([
                cls.env.ref('base.group_user').id,
                cls.env.ref('base.group_partner_manager').id,
                manager_group.id,
            ])],
        })
        cls.salesman = cls.env['res.users'].create({
            'name': 'Salesman', 'login': 'ksw_risk_salesman',
            'group_ids': [Command.set([
                cls.env.ref('base.group_user').id,
                cls.env.ref('base.group_partner_manager').id,
            ])],
        })

    @classmethod
    def _customer(cls, name):
        return cls.env['res.partner'].create({
            'name': name, 'is_company': True, 'customer_rank': 1,
            'property_account_receivable_id': cls.receivable.id,
        })

    def _overdue_invoice(self, partner, days_overdue, amount=100.0):
        due = fields.Date.context_today(self.env['res.partner']) - relativedelta(
            days=days_overdue)
        move = self.env['account.move'].create({
            'move_type': 'entry', 'journal_id': self.journal.id, 'date': due,
            'line_ids': [
                Command.create({
                    'account_id': self.receivable.id, 'partner_id': partner.id,
                    'debit': amount, 'date_maturity': due,
                }),
                Command.create({'account_id': self.income.id, 'credit': amount}),
            ],
        })
        move.action_post()
        return move

    def _grace(self, partner, days):
        partner.with_user(self.risk_manager).write({
            'x_risk_grace_policy': 'customer', 'x_risk_grace_days': days})

    def _risk(self, partners):
        partners.invalidate_recordset()
        return partners

    # ------------------------------------------------------------------
    def test_own_grace_keeps_invoice_open(self):
        self._grace(self.long_grace, 60)
        self._overdue_invoice(self.long_grace, 40)
        p = self._risk(self.long_grace)
        self.assertEqual(p.risk_invoice_unpaid, 0.0)
        self.assertEqual(p.risk_invoice_open, 100.0)

    def test_grace_ended_counts_unpaid(self):
        self._grace(self.short_grace, 10)
        self._overdue_invoice(self.short_grace, 40)
        p = self._risk(self.short_grace)
        self.assertEqual(p.risk_invoice_unpaid, 100.0)
        self.assertEqual(p.risk_invoice_open, 0.0)

    def test_each_customer_own_cutoff_in_one_batch(self):
        """Computed together, each customer still gets his own cut-off."""
        self._grace(self.long_grace, 60)
        self._grace(self.short_grace, 10)
        self._overdue_invoice(self.long_grace, 40)
        self._overdue_invoice(self.short_grace, 40)
        both = self._risk(self.long_grace | self.short_grace)
        self.assertEqual(both.mapped('risk_invoice_unpaid'), [0.0, 100.0])

    def test_no_policy_uses_company_margin(self):
        self._overdue_invoice(self.long_grace, 40)
        self.company.invoice_unpaid_margin = 50
        self.assertEqual(self._risk(self.long_grace).risk_invoice_unpaid, 0.0)
        self.assertEqual(self.long_grace.x_risk_effective_grace, 50)
        self.company.invoice_unpaid_margin = 30
        self.assertEqual(self._risk(self.long_grace).risk_invoice_unpaid, 100.0)

    def test_company_policy_ignores_own_days(self):
        self.long_grace.with_user(self.risk_manager).write({
            'x_risk_grace_policy': 'company', 'x_risk_grace_days': 90})
        self._overdue_invoice(self.long_grace, 40)
        self.assertEqual(self._risk(self.long_grace).risk_invoice_unpaid, 100.0)

    def test_unpaid_limit_raises_risk_exception(self):
        self._grace(self.long_grace, 60)
        self._grace(self.short_grace, 10)
        both = self.long_grace | self.short_grace
        both.with_user(self.risk_manager).write({
            'risk_invoice_unpaid_include': True,
            # OCA reads a 0 limit as "no limit": 1 SAR = any unpaid invoice
            'risk_invoice_unpaid_limit': 1.0,
        })
        self._overdue_invoice(self.long_grace, 40)
        self._overdue_invoice(self.short_grace, 40)
        self._risk(both)
        self.assertFalse(self.long_grace.risk_exception)
        self.assertTrue(self.short_grace.risk_exception)

    def test_pivot_lists_same_invoices(self):
        self._grace(self.long_grace, 60)
        self._overdue_invoice(self.long_grace, 40)
        model, domain = self.long_grace._get_field_risk_model_domain(
            'risk_invoice_unpaid')
        self.assertFalse(self.env[model].search(domain))
        model, domain = self.long_grace._get_field_risk_model_domain(
            'risk_invoice_open')
        self.assertTrue(self.env[model].search(domain))

    # ------------------------------------------------------------------
    def test_salesman_cannot_change_grace(self):
        with self.assertRaises(UserError):
            self.long_grace.with_user(self.salesman).write(
                {'x_risk_grace_days': 300})

    def test_contact_under_customer_inherits_grace(self):
        """A salesman adding a contact gets the customer's grace copied onto
        it by the commercial sync, without needing the manager group."""
        self._grace(self.long_grace, 60)
        contact = self.env['res.partner'].with_user(self.salesman).create({
            'name': 'Purchasing', 'parent_id': self.long_grace.id,
        })
        self.assertEqual(contact.x_risk_grace_days, 60)
        self.assertEqual(contact.x_risk_effective_grace, 60)

    def test_hand_edit_switches_to_customer(self):
        self.long_grace.with_user(self.risk_manager).write(
            {'x_risk_grace_policy': 'bas', 'x_risk_grace_days': 60})
        self.long_grace.with_user(self.risk_manager).write(
            {'x_risk_grace_days': 45})
        self.assertEqual(self.long_grace.x_risk_grace_policy, 'customer')

    # ------------------------------------------------------------------
    def _bas(self, code, invoice_days, credit_days, partner):
        return self.env['ksw.bas.customer'].create({
            'bas_code': code, 'name_ar': code,
            'invoice_term_days': invoice_days, 'credit_term_days': credit_days,
            'partner_id': partner.id,
        })

    def test_bas_prefills_gap_between_term_and_stop(self):
        self._bas('KSWT0001', 30, 90, self.long_grace)
        self.env['ksw.bas.customer']._apply_risk_grace()
        self.assertEqual(self.long_grace.x_risk_grace_policy, 'bas')
        self.assertEqual(self.long_grace.x_risk_grace_days, 60)

    def test_bas_follows_changes_until_edited_by_hand(self):
        rec = self._bas('KSWT0002', 30, 90, self.long_grace)
        Bas = self.env['ksw.bas.customer']
        Bas._apply_risk_grace()
        rec.credit_term_days = 120
        Bas._apply_risk_grace()
        self.assertEqual(self.long_grace.x_risk_grace_days, 90)
        self.long_grace.with_user(self.risk_manager).write(
            {'x_risk_grace_days': 45})
        rec.credit_term_days = 300
        Bas._apply_risk_grace()
        self.assertEqual(self.long_grace.x_risk_grace_days, 45)
        self.assertEqual(self.long_grace.x_risk_grace_policy, 'customer')

    def test_bas_leaves_explicit_company_default_alone(self):
        self.long_grace.with_user(self.risk_manager).write(
            {'x_risk_grace_policy': 'company'})
        self._bas('KSWT0003', 30, 90, self.long_grace)
        self.env['ksw.bas.customer']._apply_risk_grace()
        self.assertEqual(self.long_grace.x_risk_grace_policy, 'company')

    def test_bas_two_accounts_most_lenient_wins(self):
        self._bas('KSWT0004', 15, 45, self.long_grace)
        self._bas('KSWT0005', 30, 120, self.long_grace)
        self.env['ksw.bas.customer']._apply_risk_grace()
        self.assertEqual(self.long_grace.x_risk_grace_days, 90)

    def test_bas_stop_below_term_gives_zero_grace(self):
        self._bas('KSWT0006', 60, 30, self.long_grace)
        self.env['ksw.bas.customer']._apply_risk_grace()
        self.assertEqual(self.long_grace.x_risk_grace_days, 0)
