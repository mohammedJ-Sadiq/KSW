"""BAS's payment term (COD10.INVDAYS) onto the Odoo contact.

INVDAYS, not LIMT_DAYS: on 86,905 credit documents since Jan 2026 the
document's own DUTY_DAY equals INVDAYS on 99.3% of them (verified 2026-10-04).
"""
from odoo.tests import TransactionCase, tagged


# post_install: `property_payment_term_id` comes from `account`, which this
# module does not depend on.
@tagged('post_install', '-at_install')
class TestPaymentTerms(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Customer = self.env['ksw.bas.customer']
        self.partner = self.env['res.partner'].create({
            'name': 'Zz Term Test Co', 'customer_rank': 1,
            'x_client_account_number': '1203999101',
        })
        self.rec = self.Customer.create({
            'bas_code': '1203999101', 'name_en': 'Zz Term Test Co',
            'invoice_term_days': 60,
        })

    def _days(self, term):
        return term.line_ids.nb_days

    def test_the_bas_term_lands_on_the_contact(self):
        self.Customer._apply_payment_terms()
        self.assertEqual(self._days(self.partner.property_payment_term_id), 60)
        self.assertTrue(self.partner.x_payment_term_from_bas)

    def test_an_existing_term_is_reused_not_duplicated(self):
        Term = self.env['account.payment.term']
        before = Term.search_count([])
        self.Customer._apply_payment_terms()
        term = self.partner.property_payment_term_id
        self.Customer._apply_payment_terms()
        self.assertEqual(self.partner.property_payment_term_id, term)
        # 60 Days already exists in this database (l10n_sa); a second pass
        # must certainly not add another.
        self.assertLessEqual(Term.search_count([]), before + 1)

    def test_an_unusual_bas_term_is_created_as_bas_has_it(self):
        self.rec.invoice_term_days = 2000
        self.Customer._apply_payment_terms()
        term = self.partner.property_payment_term_id
        self.assertEqual(self._days(term), 2000)
        self.assertEqual(term.line_ids.delay_type, 'days_after')
        self.assertEqual(term.line_ids.value_amount, 100)

    def test_a_change_in_bas_follows(self):
        self.Customer._apply_payment_terms()
        self.rec.invoice_term_days = 90
        self.Customer._apply_payment_terms()
        self.assertEqual(self._days(self.partner.property_payment_term_id), 90)

    def test_a_term_set_by_hand_in_odoo_is_kept(self):
        self.Customer._apply_payment_terms()
        manual = self.env.ref('account.account_payment_term_15days')
        self.partner.property_payment_term_id = manual
        self.assertFalse(self.partner.x_payment_term_from_bas)
        self.rec.invoice_term_days = 90
        self.Customer._apply_payment_terms()
        self.assertEqual(self.partner.property_payment_term_id, manual)

    def test_a_contact_that_already_had_a_term_is_left_alone(self):
        manual = self.env.ref('account.account_payment_term_15days')
        self.partner.property_payment_term_id = manual
        self.Customer._apply_payment_terms()
        self.assertEqual(self.partner.property_payment_term_id, manual)

    def test_zero_days_is_immediate(self):
        self.rec.invoice_term_days = 0
        self.Customer._apply_payment_terms()
        self.assertEqual(self._days(self.partner.property_payment_term_id), 0)

    def test_every_contact_on_the_account_gets_it(self):
        # A name match linked one contact; the notes were issued to another
        # that carries the same account number.
        linked = self.env['res.partner'].create({'name': 'Zz Term Linked'})
        self.rec.partner_id = linked
        self.Customer._apply_payment_terms()
        self.assertEqual(self._days(linked.property_payment_term_id), 60)
        self.assertEqual(self._days(self.partner.property_payment_term_id), 60)
