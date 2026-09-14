"""Tests for the per-employee rate exception (``ksw.pay.employee.rate``).

What is actually being guarded here:

* the standard rate keeps applying to everybody who has no exception —
  the negative half is the one that matters, since a broken resolver that
  returns the exception to everyone would pass every positive test;
* the exception is **dated**, so an entry in an earlier month computes at
  the earlier rate. That is the whole reason the model has dates;
* an option-specific exception beats a component-wide one;
* an entry already recorded follows a later rate change while its batch is
  open, and an approved (locked) month does not;
* only the Administrator may write one.
"""
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase

from .test_pay_entry import PayEntryCommon


class TestEmployeeRate(PayEntryCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.c_friday = cls.env.ref('KSW_commissions.pay_component_friday')
        cls.Rate = cls.env['ksw.pay.employee.rate'].sudo()

    def _rate(self, employee=None, component=None, **kwargs):
        vals = {
            'employee_id': (employee or self.emp).id,
            'component_id': (component or self.c_friday).id,
            'rate': 150.0,
            'date_from': '2028-01-01',
        }
        vals.update(kwargs)
        return self.Rate.create(vals)

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------
    def test_01_standard_rate_without_an_exception(self):
        """Friday is 100 a day for someone with no rate of their own."""
        entry = self._entry(self._batch(self.c_friday), quantity=3.0)
        self.assertAlmostEqual(entry.rate, 100.0, places=4)
        self.assertAlmostEqual(entry.amount, 300.0, places=2)

    def test_02_employee_with_an_exception_is_paid_it(self):
        self._rate()
        entry = self._entry(self._batch(self.c_friday), quantity=3.0)
        self.assertAlmostEqual(entry.rate, 150.0, places=4)
        self.assertAlmostEqual(entry.amount, 450.0, places=2)

    def test_03_exception_does_not_leak_to_other_employees(self):
        """The half that a broken resolver would get wrong."""
        self._rate()
        batch = self._batch(self.c_friday)
        mine = self._entry(batch, quantity=3.0)
        theirs = self._entry(batch, employee=self.emp2, quantity=3.0)
        self.assertAlmostEqual(mine.amount, 450.0, places=2)
        self.assertAlmostEqual(theirs.amount, 300.0, places=2)

    def test_04_exception_does_not_leak_to_other_components(self):
        self._rate()
        entry = self._entry(self._batch(self.c_meals),
                            option_id=self.o_lunch.id, quantity=2.0)
        self.assertAlmostEqual(entry.rate, self.o_lunch.rate, places=4)

    def test_05_earlier_month_keeps_the_earlier_rate(self):
        """A raise in July does not restate June."""
        self._rate(date_from='2028-07-01')
        june = self._entry(
            self._batch(self.c_friday, period='2028-06-01'),
            date='2028-06-02', quantity=1.0)
        july = self._entry(
            self._batch(self.c_friday, period='2028-07-01'),
            date='2028-07-07', quantity=1.0)
        self.assertAlmostEqual(june.amount, 100.0, places=2)
        self.assertAlmostEqual(july.amount, 150.0, places=2)

    def test_06_closed_off_exception_stops_applying(self):
        self._rate(date_from='2028-01-01', date_to='2028-06-30')
        after = self._entry(
            self._batch(self.c_friday, period='2028-07-01'),
            date='2028-07-07', quantity=1.0)
        self.assertAlmostEqual(after.amount, 100.0, places=2)

    def test_07_monthly_component_resolves_on_the_period(self):
        """A component with no date per occurrence uses the batch month."""
        self._rate(component=self.c_meals, option_id=False, rate=30.0,
                   date_from='2028-07-01')
        entry = self._entry(
            self._batch(self.c_meals, period='2028-07-01'),
            option_id=self.o_lunch.id, quantity=2.0)
        self.assertAlmostEqual(entry.rate, 30.0, places=4)
        self.assertAlmostEqual(entry.amount, 60.0, places=2)

    def test_08_option_specific_beats_component_wide(self):
        self._rate(component=self.c_meals, rate=30.0)
        self._rate(component=self.c_meals, option_id=self.o_lunch.id,
                   rate=45.0)
        batch = self._batch(self.c_meals)
        lunch = self._entry(batch, option_id=self.o_lunch.id, quantity=1.0)
        breakfast = self._entry(batch, option_id=self.o_breakfast.id,
                                quantity=1.0)
        self.assertAlmostEqual(lunch.rate, 45.0, places=4)
        self.assertAlmostEqual(breakfast.rate, 30.0, places=4)

    def test_09_archived_exception_is_ignored(self):
        rate = self._rate()
        rate.active = False
        entry = self._entry(self._batch(self.c_friday), quantity=1.0)
        self.assertAlmostEqual(entry.amount, 100.0, places=2)

    def test_10_manual_override_still_wins(self):
        """amount_override is the 'just this once' answer and outranks both."""
        self._rate()
        entry = self._entry(self._batch(self.c_friday), quantity=3.0,
                            amount_override=500.0)
        self.assertAlmostEqual(entry.amount, 500.0, places=2)
        self.assertAlmostEqual(entry.amount_computed, 450.0, places=2)

    # ------------------------------------------------------------------
    # Keeping open entries in step
    # ------------------------------------------------------------------
    def test_11_existing_draft_entry_follows_a_new_exception(self):
        """The entry is typed first, the exception added after."""
        entry = self._entry(self._batch(self.c_friday), quantity=3.0)
        self.assertAlmostEqual(entry.amount, 300.0, places=2)
        self._rate()
        self.env.flush_all()
        self.assertAlmostEqual(entry.amount, 450.0, places=2)

    def test_12_existing_draft_entry_follows_an_edited_exception(self):
        rate = self._rate()
        entry = self._entry(self._batch(self.c_friday), quantity=3.0)
        rate.rate = 200.0
        self.env.flush_all()
        self.assertAlmostEqual(entry.amount, 600.0, places=2)

    def test_13_deleting_the_exception_restores_the_standard_rate(self):
        rate = self._rate()
        entry = self._entry(self._batch(self.c_friday), quantity=3.0)
        rate.unlink()
        self.env.flush_all()
        self.assertAlmostEqual(entry.amount, 300.0, places=2)

    def test_14_a_locked_period_is_never_restated(self):
        """An approved month has been signed off and, once exported, paid."""
        batch = self._batch(self.c_friday)
        entry = self._entry(batch, quantity=3.0)
        # Worked out and stored before the month closes, as it is in life.
        self.assertAlmostEqual(entry.amount, 300.0, places=2)
        self.env.flush_all()
        Run = self.env['ksw.pay.run'].sudo()
        run = Run.search([('period', '=', self.period)], limit=1) \
            or Run.create({'period': self.period})
        run.write({'state': 'approved'})
        self._rate()
        self.env.flush_all()
        self.assertAlmostEqual(entry.amount, 300.0, places=2)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def test_15_only_quantity_times_rate_components(self):
        with self.assertRaises(ValidationError):
            self._rate(component=self.c_overtime)   # salary-derived
        with self.assertRaises(ValidationError):
            self._rate(component=self.c_mobile)     # fixed amount

    def test_16_option_must_belong_to_the_component(self):
        with self.assertRaises(ValidationError):
            self._rate(component=self.c_friday, option_id=self.o_lunch.id)

    def test_17_overlapping_windows_are_refused(self):
        self._rate(date_from='2028-01-01', date_to='2028-12-31')
        with self.assertRaises(ValidationError):
            self._rate(date_from='2028-06-01')

    def test_18_consecutive_windows_are_fine(self):
        self._rate(date_from='2028-01-01', date_to='2028-06-30', rate=120.0)
        later = self._rate(date_from='2028-07-01', rate=150.0)
        self.assertTrue(later.id)

    def test_19_negative_rate_and_backwards_dates(self):
        with self.assertRaises(ValidationError):
            self._rate(rate=-1.0)
        with self.assertRaises(ValidationError):
            self._rate(date_from='2028-07-01', date_to='2028-01-01')

    def test_20_standard_rate_is_shown_next_to_the_exception(self):
        rate = self._rate()
        self.assertAlmostEqual(rate.standard_rate, 100.0, places=2)
        meal = self._rate(component=self.c_meals,
                          option_id=self.o_lunch.id, rate=45.0)
        self.assertAlmostEqual(meal.standard_rate, self.o_lunch.rate, places=2)

    # ------------------------------------------------------------------
    # It explains itself
    # ------------------------------------------------------------------
    def test_21_explanation_says_the_rate_is_an_exception(self):
        self._rate(reason='Agreed with the GM')
        entry = self._entry(self._batch(self.c_friday), quantity=3.0)
        html = entry.explanation
        self.assertIn('own rate', html)
        self.assertIn('150.00', html)
        self.assertIn('100.00', html)
        self.assertIn('Agreed with the GM', html)

    def test_22_explanation_is_silent_without_one(self):
        entry = self._entry(self._batch(self.c_friday), quantity=3.0)
        self.assertNotIn('own rate', entry.explanation)


class TestEmployeeRateAccess(TransactionCase):
    """What a unit is worth is not the entry-recorder's decision."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.component = cls.env.ref('KSW_commissions.pay_component_friday')
        cls.employee = cls.env['hr.employee'].sudo().create(
            {'name': 'Rate Access Emp'})
        cls.supervisor = cls.env['res.users'].sudo().create({
            'name': 'Rate Supervisor', 'login': 'rate_supervisor',
            'group_ids': [(4, cls.env.ref(
                'KSW_commissions.group_commission_supervisor').id)],
        })
        cls.administrator = cls.env['res.users'].sudo().create({
            'name': 'Rate Administrator', 'login': 'rate_administrator',
            'group_ids': [(4, cls.env.ref(
                'KSW_commissions.group_commission_officer').id)],
        })

    def _vals(self):
        return {
            'employee_id': self.employee.id,
            'component_id': self.component.id,
            'rate': 150.0,
        }

    def test_01_supervisor_cannot_create(self):
        with self.assertRaises(AccessError):
            self.env['ksw.pay.employee.rate'].with_user(
                self.supervisor).create(self._vals())

    def test_02_supervisor_cannot_edit(self):
        rate = self.env['ksw.pay.employee.rate'].sudo().create(self._vals())
        with self.assertRaises(AccessError):
            rate.with_user(self.supervisor).write({'rate': 999.0})

    def test_03_supervisor_may_read_it(self):
        """He has to: the amount on his own entry is explained by it."""
        rate = self.env['ksw.pay.employee.rate'].sudo().create(self._vals())
        self.assertAlmostEqual(
            rate.with_user(self.supervisor).rate, 150.0, places=2)

    def test_04_administrator_may_maintain_it(self):
        rate = self.env['ksw.pay.employee.rate'].with_user(
            self.administrator).create(self._vals())
        rate.write({'rate': 175.0})
        self.assertAlmostEqual(rate.rate, 175.0, places=2)
