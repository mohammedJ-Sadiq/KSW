"""The BAS rate import has to work on a database without KSW_bas_gl_import.

It used to refuse outright unless `x_bas_code` existed on partners and
products -- a field only KSW_bas_gl_import declares, which production does
not have and should not get. So the one button that seeds the rate register
could never run where the rates are actually needed.

Now a BAS client code resolves through the connector's own mirror
(`ksw.bas.customer.partner_id`, filled by Match / Create Contacts) and a BAS
item code through the product's Internal Reference. Where `x_bas_code`
exists it still wins.
"""
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestRateImportMatching(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Rate = self.env['ksw.water.rate']
        self.partner = self.env['res.partner'].create({'name': 'Zz Mirror Client'})
        self.env['ksw.bas.customer'].create({
            'bas_code': '1203888001', 'name_en': 'Zz Mirror Client',
            'partner_id': self.partner.id,
        })
        self.product = self.env['product.product'].create({
            'name': 'Zz Sweet Water (test)', 'type': 'consu',
            'default_code': '99032',
        })

    def test_a_client_resolves_through_the_connector_mirror(self):
        self.assertEqual(self.Rate._bas_partner_map().get('1203888001'), self.partner)

    def test_a_product_resolves_through_its_internal_reference(self):
        self.assertEqual(self.Rate._bas_product_map().get('99032'), self.product)

    def test_x_bas_code_still_wins_where_it_exists(self):
        """Dev has KSW_bas_gl_import, whose mapping is by code rather than by
        name -- it must not be displaced by the mirror's name match."""
        if 'x_bas_code' not in self.env['res.partner']._fields:
            self.skipTest('KSW_bas_gl_import not installed here')
        other = self.env['res.partner'].create({
            'name': 'Zz GL Client', 'x_bas_code': '1203888001'})
        self.assertEqual(self.Rate._bas_partner_map().get('1203888001'), other)

    def test_nothing_to_match_says_what_to_do_before_touching_bas(self):
        """And says it without a network round trip: the guard runs before
        the connector is ever asked for a connection."""
        with patch.object(type(self.Rate), '_bas_partner_map', return_value={}), \
                patch.object(type(self.env['ksw.bas.connector']), '_bas_connect',
                             side_effect=AssertionError('must not connect')):
            with self.assertRaises(UserError) as caught:
                self.Rate.sudo().action_import_from_bas()
        self.assertIn('Match / Create Contacts', str(caught.exception))
