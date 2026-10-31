"""A customer created in Odoo gets «الرد المضاعف» = 1 (Oct 2026).

Clients used to exist only for BAS customer accounts, so a customer added in
Odoo had no multiplier and could not even be picked on a change request.
"""
from datetime import timedelta

from odoo import fields
from odoo.tests import TransactionCase, new_test_user, tagged


@tagged('post_install', '-at_install')
class TestNewCustomerMultiplier(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.today = fields.Date.context_today(cls.env['res.users'])
        cls.Client = cls.env['ksw.client.multiplier']

    def _client(self, partners):
        return self.Client.search([('partner_id', 'in', partners.ids)])

    def test_a_new_customer_starts_at_one(self):
        partner = self.env['res.partner'].create({'name': 'New Odoo Client', 'customer_rank': 1})
        client = self._client(partner)
        self.assertEqual(len(client), 1)
        self.assertFalse(client.customer_id)
        self.assertEqual(client.current_multiplier, 1.0)
        self.assertEqual(client.rate_ids.source, 'default')
        self.assertEqual(client.name, 'New Odoo Client')
        self.assertEqual(self.Client._multiplier_for_partners(partner, self.today),
                         {partner.id: 1.0})

    def test_a_contact_becoming_a_customer_starts_at_one(self):
        partner = self.env['res.partner'].create({'name': 'Was a Contact'})
        self.assertFalse(self._client(partner))
        partner._increase_rank('customer_rank')
        self.assertEqual(self._client(partner).current_multiplier, 1.0)

    def test_contacts_vendors_and_child_addresses_get_nothing(self):
        Partner = self.env['res.partner']
        plain = Partner.create({'name': 'Just a Contact'})
        vendor = Partner.create({'name': 'A Vendor', 'supplier_rank': 1})
        parent = Partner.create({'name': 'Parent Co', 'customer_rank': 1})
        child = Partner.create({'name': 'Site', 'parent_id': parent.id, 'customer_rank': 1})
        self.assertFalse(self._client(plain | vendor | child))

    def test_a_bas_account_number_is_not_required(self):
        """A BAS account with no client in Odoo yet does not hold the customer
        back: it starts at 1 like any other."""
        partner = self.env['res.partner'].create({
            'name': 'BAS Client', 'customer_rank': 1, 'x_client_account_number': '1203ZZ900'})
        self.assertEqual(self._client(partner).current_multiplier, 1.0)

    def test_a_contact_takes_its_bas_accounts_client(self):
        """Where its BAS account already has a client, the contact gets that
        one -- named after the contact, carrying BAS's value -- not a second."""
        customer = self.env['ksw.bas.customer'].create({
            'bas_code': '1203ZZ910', 'name_ar': 'BAS Name'})
        bas_client = self.Client.create({'customer_id': customer.id})
        self.env['ksw.client.multiplier.rate'].create({
            'client_id': bas_client.id, 'multiplier': 2.0, 'source': 'opening'})
        partner = self.env['res.partner'].create({
            'name': 'Odoo Name', 'customer_rank': 1, 'x_client_account_number': '1203ZZ910'})
        self.assertEqual(self._client(partner), bas_client)
        self.assertEqual(bas_client.name, 'Odoo Name')
        self.assertEqual(bas_client.display_name, '[1203ZZ910] Odoo Name')
        self.assertEqual(bas_client.current_multiplier, 2.0)

    def test_a_ledger_account_contact_gets_nothing(self):
        till = self.env['res.partner'].create({
            'name': 'Till', 'customer_rank': 1, 'x_client_account_number': '1201ZZ900'})
        self.assertFalse(self._client(till))

    def test_it_can_be_changed_like_any_client(self):
        supervisor = new_test_user(
            self.env, 'nc_supervisor',
            groups='base.group_user,KSW_water_delivery.group_water_dispatcher')
        gm = new_test_user(self.env, 'nc_gm', groups='base.group_user')
        gm_emp = self.env['hr.employee'].create({'name': 'NC GM', 'user_id': gm.id})
        dept = self.env['hr.department'].create({'name': 'NC Ops', 'x_gm_id': gm_emp.id})
        self.env['hr.employee'].create({'name': 'NC Sup', 'user_id': supervisor.id,
                                        'department_id': dept.id})
        partner = self.env['res.partner'].create({'name': 'To Change', 'customer_rank': 1})
        client = self._client(partner)
        req = self.env['ksw.client.multiplier.request'].with_user(supervisor).create({
            'client_id': client.id, 'new_multiplier': 1.5,
            'effective_date': self.today, 'reason': 'Far site'})
        req.with_user(supervisor).action_submit()
        req.with_user(gm).action_approve()
        self.assertEqual(client.current_multiplier, 1.5)
        self.assertEqual(self.Client._multiplier_for_partners(
            partner, self.today - timedelta(days=1)), {partner.id: 1.0})

    def test_a_later_bas_account_adopts_the_record(self):
        partner = self.env['res.partner'].create({'name': 'Joins BAS', 'customer_rank': 1})
        client = self._client(partner)
        customer = self.env['ksw.bas.customer'].create({
            'bas_code': '1203ZZ901', 'name_ar': 'Joins BAS', 'partner_id': partner.id})
        adopted = self.Client._adopt_odoo_clients(customer)
        self.assertEqual(adopted, customer)
        self.assertEqual(client.customer_id, customer)
        self.assertFalse(client.rate_ids, 'the untouched default makes way for BAS\'s value')
        self.assertEqual(self._client(partner), client, 'still one record')

    def test_an_approved_change_survives_adoption(self):
        partner = self.env['res.partner'].create({'name': 'Decided in Odoo', 'customer_rank': 1})
        client = self._client(partner)
        self.env['ksw.client.multiplier.rate'].create({
            'client_id': client.id, 'multiplier': 1.7, 'date_from': self.today,
            'source': 'request'})
        customer = self.env['ksw.bas.customer'].create({
            'bas_code': '1203ZZ902', 'name_ar': 'Decided', 'partner_id': partner.id})
        self.Client._adopt_odoo_clients(customer)
        self.assertEqual(sorted(client.rate_ids.mapped('multiplier')), [1.0, 1.7])

    def test_the_backfill_defaults_customers_but_not_ledger_accounts(self):
        Partner = self.env['res.partner'].with_context(
            # the automatic default must not pre-empt what is being tested
            tracking_disable=True)
        customer = Partner.create({'name': 'H Customer', 'x_client_account_number': '1203ZZ950'})
        till = Partner.create({'name': 'Branch Till', 'x_client_account_number': '1201ZZ950'})
        (customer | till).write({'customer_rank': 1})
        self.Client.search([('partner_id', 'in', (customer | till).ids)]).rate_ids.unlink()
        self.Client.search([('partner_id', 'in', (customer | till).ids)]).unlink()
        own = self.env.company.partner_id
        own.customer_rank = 1

        done = self.Client._backfill_existing_customers()

        self.assertIn(customer, done.partner_id)
        self.assertNotIn(till, done.partner_id)
        self.assertNotIn(own, done.partner_id)
        self.assertEqual(self._client(customer).current_multiplier, 1.0)
        self.assertFalse(self.Client._backfill_existing_customers().filtered(
            lambda c: c.partner_id == customer), 'running it twice adds nothing')

    def test_the_customer_form_shows_the_multiplier(self):
        partner = self.env['res.partner'].create({'name': 'Form Client', 'customer_rank': 1})
        self.assertEqual(partner.x_multiplier, 1.0)
        action = partner.action_open_multiplier()
        self.assertEqual((action['res_model'], action['res_id']),
                         ('ksw.client.multiplier', self._client(partner).id))
        arch = self.env['res.partner'].get_views([(False, 'form')])['views']['form']['arch']
        self.assertIn('x_multiplier', arch)
