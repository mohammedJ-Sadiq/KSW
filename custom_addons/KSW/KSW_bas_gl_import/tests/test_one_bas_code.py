"""One BAS account code per contact: the Reference (Oct 2026)."""
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestOneBasCode(TransactionCase):

    def test_both_names_are_the_reference(self):
        partner = self.env['res.partner'].create({'name': 'One Code', 'x_bas_code': '120399001'})
        self.assertEqual(partner.ref, '120399001')
        self.assertEqual(partner.x_client_account_number, '120399001')
        partner.x_client_account_number = '120399002'
        self.assertEqual(partner.x_bas_code, '120399002')

    def test_either_name_finds_the_contact(self):
        partner = self.env['res.partner'].create({'name': 'Found', 'ref': '120399003'})
        Partner = self.env['res.partner']
        self.assertEqual(Partner.search([('x_bas_code', '=', '120399003')]), partner)
        self.assertEqual(Partner.search([('x_client_account_number', '=', '120399003')]), partner)

    def test_a_duplicated_contact_does_not_take_the_code(self):
        partner = self.env['res.partner'].create({'name': 'Original', 'ref': '120399004'})
        self.assertFalse(partner.copy().ref)
