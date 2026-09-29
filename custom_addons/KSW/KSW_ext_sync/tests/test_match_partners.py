"""Match / Create Contacts: the step that turns 780 mirrored BAS customers
into contacts a picker can show.

Two defects found preparing the water delivery deploy (2026-09-29), when
production had 780 BAS customers mirrored and 0 linked:

* a MATCHED contact never got `customer_rank`, so every picker filtered on
  `customer_rank > 0` (water delivery, workshop, `res_partner_search_mode`)
  kept hiding exactly the clients the match had just found;
* a name match on a contact already carrying the SAME account fell through
  to create(), so re-running the button duplicated contacts.
"""
from odoo.tests import TransactionCase, tagged


# post_install: `customer_rank` comes from `account`, which is not one of
# this module's dependencies, so it is absent from the at_install registry.
@tagged('post_install', '-at_install')
class TestMatchPartners(TransactionCase):

    def setUp(self):
        super().setUp()
        # Isolate from the real mirror rows in the database.
        self.env['ksw.bas.customer'].search([('partner_id', '=', False)]).write(
            {'is_stopped': True})
        self.Customer = self.env['ksw.bas.customer']

    def _bas(self, code, name):
        return self.Customer.create({
            'bas_code': code, 'name_en': name, 'name_ar': name, 'is_stopped': False,
        })

    def test_a_matched_contact_becomes_a_customer(self):
        partner = self.env['res.partner'].create({
            'name': 'Zz Matcher Test Co', 'customer_rank': 0})
        rec = self._bas('1203999001', 'Zz Matcher Test Co')

        self.Customer.action_match_or_create_partners()

        self.assertEqual(rec.partner_id, partner)
        self.assertEqual(partner.customer_rank, 1)
        self.assertEqual(partner.x_client_account_number, '1203999001')

    def test_an_existing_rank_is_never_lowered(self):
        partner = self.env['res.partner'].create({
            'name': 'Zz Busy Customer', 'customer_rank': 7})
        self._bas('1203999002', 'Zz Busy Customer')

        self.Customer.action_match_or_create_partners()
        self.assertEqual(partner.customer_rank, 7)

    def test_a_contact_already_on_this_account_is_linked_not_duplicated(self):
        partner = self.env['res.partner'].create({
            'name': 'Zz Linked By Hand', 'x_client_account_number': '1203999003'})
        rec = self._bas('1203999003', 'Zz Linked By Hand')

        self.Customer.action_match_or_create_partners()

        self.assertEqual(rec.partner_id, partner)
        self.assertEqual(
            self.env['res.partner'].search_count([('name', '=', 'Zz Linked By Hand')]), 1)

    def test_a_contact_on_another_account_is_not_hijacked(self):
        """Same name, different BAS account: two businesses, two contacts."""
        other = self.env['res.partner'].create({
            'name': 'Zz Same Name', 'x_client_account_number': '1203111111'})
        rec = self._bas('1203999004', 'Zz Same Name')

        self.Customer.action_match_or_create_partners()

        self.assertNotEqual(rec.partner_id, other)
        self.assertTrue(rec.partner_created)
        self.assertEqual(rec.partner_id.customer_rank, 1)
        self.assertEqual(other.x_client_account_number, '1203111111')
