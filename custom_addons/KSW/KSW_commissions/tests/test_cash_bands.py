# -*- coding: utf-8 -*-
"""The cash-load amount bands.

A cash sale is booked to the till, not the customer, so the account rate
describes the branch rather than the journey. The factor is read off the
line amount instead. These tests pin the boundaries — the rule is sharp at
the edges on purpose, so the edges are exactly what can go wrong.
"""
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class TestCashBands(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Band = cls.env['ksw.pay.cash.band'].sudo()

    def _factor(self, amount):
        """Resolve an amount through the generated SQL, not a Python copy —
        the SQL is what the import actually runs."""
        case = self.Band._sql_case(str(float(amount)))
        self.env.cr.execute('SELECT %s AS f' % case)
        return float(self.env.cr.fetchone()[0])

    # ------------------------------------------------------------------
    def test_the_shipped_bands_match_the_business_rule(self):
        for amount, expected in (
                (329.99, 0.0), (330, 0.75), (375, 0.75),
                (376, 1.0), (450, 1.0),
                (451, 1.5), (550, 1.5),
                (551, 2.0), (650, 2.0),
                (651, 2.5), (750, 2.5),
                (751, 3.0), (850, 3.0),
                (851, 3.5), (950, 3.5),
                (951, 4.0), (5000, 4.0)):
            self.assertAlmostEqual(
                self._factor(amount), expected, places=4,
                msg='amount %s should weight %s' % (amount, expected))

    def test_below_the_first_band_earns_nothing(self):
        """Not a floor of 0.75 — an unmeasured run pays nothing."""
        self.assertEqual(self._factor(100), 0.0)
        self.assertEqual(self._factor(329), 0.0)

    def test_editing_a_band_does_not_trip_its_own_overlap_check(self):
        """The check must not see the record it is checking, nor treat the
        open-ended top band as overlapping everything below it."""
        for band in self.Band.search([]):
            band.write({'factor': band.factor})      # must not raise

    def test_the_open_ended_band_does_not_swallow_the_others(self):
        top = self.Band.search([('amount_to', '=', 0)], limit=1)
        self.assertTrue(top, 'There should be an open-ended top band.')
        below = self.Band.search([('amount_to', '!=', 0)], limit=1)
        below.write({'factor': below.factor})        # must not raise

    def test_an_overlapping_band_is_refused(self):
        with self.assertRaises(ValidationError):
            self.Band.create({'amount_from': 400, 'amount_to': 500,
                              'factor': 9.0})

    def test_a_band_that_ends_before_it_starts_is_refused(self):
        with self.assertRaises(ValidationError):
            self.Band.create({'amount_from': 2000, 'amount_to': 1000,
                              'factor': 1.0})

    def test_a_gap_between_bands_is_allowed_and_earns_nothing(self):
        """Deliberate: a gap is a business choice, not a mistake to guess
        a value for."""
        self.Band.search([]).unlink()
        self.Band.create({'amount_from': 100, 'amount_to': 200,
                          'factor': 1.0})
        self.Band.create({'amount_from': 400, 'amount_to': 500,
                          'factor': 2.0})
        self.assertEqual(self._factor(150), 1.0)
        self.assertEqual(self._factor(300), 0.0)
        self.assertEqual(self._factor(450), 2.0)

    def test_no_bands_at_all_resolves_to_nothing(self):
        self.Band.search([]).unlink()
        self.assertIsNone(self.Band._sql_case('s.AMOUNT'))

    def test_the_cash_accounts_are_configurable(self):
        self.assertIn('1201020006', self.Band._cash_accounts())
        self.env['ir.config_parameter'].sudo().set_param(
            'ksw_commissions.bas_cash_accounts', ' 999 , 888 ')
        self.assertEqual(self.Band._cash_accounts(), ('999', '888'))
