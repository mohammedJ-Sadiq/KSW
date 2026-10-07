"""A holiday bonus is paid for its holiday; a Friday count for its Fridays.

KSWCO PB000081 (Oct 2026): MD ANOWAR HOSSAIN was back from vacation on 10
September, National Day was the 23rd, and the September National Day Bonus
still flagged him "on vacation" — the row had no date, so the hold could only
say "he was away for part of this month". The day is already known (Time Off
-> Public Holidays), so the row is dated from there and judged like any
dated row. A Friday allowance is a count, not a day, so it is measured
against the Fridays left after the return instead.
"""
from datetime import date, datetime

from odoo.exceptions import UserError

from .test_vacation_hold import SEP, OCT, VacationHoldCommon

NATIONAL_DAY = date(2026, 9, 23)


class TestPaidDay(VacationHoldCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.bonus = env.ref('KSW_commissions.pay_component_bonus_national')
        cls.friday = env.ref('KSW_commissions.pay_component_friday')
        # Created before any leave: a holiday over a date somebody is on
        # leave for trips hr_holidays' _reevaluate_leaves (Pitfall #164).
        cls.holiday = env['resource.calendar.leaves'].sudo().create({
            'name': 'National Day - 2026',
            'date_from': datetime(2026, 9, 23, 3, 0),
            'date_to': datetime(2026, 9, 23, 15, 0),
        })

    def _bonus_entry(self, batch, **kwargs):
        return self._entry(batch, amount=100.0, **kwargs)

    # ------------------------------------------------------------------
    def test_the_holiday_is_tagged_from_its_name(self):
        self.assertEqual(self.holiday.x_pay_occasion, 'national_day')

    def test_the_bonus_row_takes_the_holidays_date(self):
        entry = self._bonus_entry(self._batch(SEP, self.bonus))
        self.assertEqual(entry.date, NATIONAL_DAY)

    def test_back_before_the_holiday_is_paid_and_not_flagged(self):
        """The PB000081 case: back on 10 Sep, National Day on the 23rd."""
        self._leave(date(2026, 7, 11), date(2026, 9, 8),
                    return_date=date(2026, 9, 10))
        batch = self._batch(SEP, self.bonus)
        entry = self._bonus_entry(batch)
        self.assertFalse(entry.x_vacation_hold)
        self.assertFalse(batch._held_entries())

    def test_still_away_on_the_holiday_is_refused(self):
        self._leave(date(2026, 8, 1), date(2026, 9, 24),
                    return_date=date(2026, 9, 25))
        with self.assertRaises(UserError):
            self._bonus_entry(self._batch(SEP, self.bonus))

    def test_a_typed_date_is_put_back_to_the_holiday(self):
        entry = self._bonus_entry(self._batch(SEP, self.bonus))
        entry.with_user(self.officer).write({'date': date(2026, 9, 5)})
        self.assertEqual(entry.date, NATIONAL_DAY)

    def test_a_month_without_the_holiday_is_refused(self):
        """National Day Bonus recorded in October — the batch's first
        month on KSWCO before it was corrected."""
        with self.assertRaises(UserError):
            self._bonus_entry(self._batch(OCT, self.bonus))

    def test_moving_the_batch_to_a_month_without_it_is_refused(self):
        batch = self._batch(SEP, self.bonus)
        self._bonus_entry(batch)
        with self.assertRaises(UserError):
            batch.with_user(self.officer).write({'period': OCT})

    def test_moving_the_batch_into_the_holidays_month_dates_the_rows(self):
        batch = self._batch(SEP, self.bonus)
        entry = self._bonus_entry(batch)
        # Off the calendar for a moment, as an old undated row would be.
        entry.sudo().write({'date': False})
        self.holiday.sudo().x_pay_occasion = 'national_day'
        batch.with_user(self.officer).write({'period': SEP})
        self.assertEqual(entry.date, NATIONAL_DAY)

    # ------------------------------------------------------------------
    # Fridays: back on Thu 10 Sep 2026 leaves 11, 18 and 25 Sep.
    def _back_on_the_10th(self):
        self._leave(date(2026, 7, 11), date(2026, 9, 8),
                    return_date=date(2026, 9, 10))

    def test_fridays_after_the_return_are_paid_and_not_flagged(self):
        self._back_on_the_10th()
        entry = self._entry(self._batch(SEP, self.friday), quantity=3.0)
        self.assertFalse(entry.x_vacation_hold)

    def test_more_fridays_than_he_was_back_for_are_refused(self):
        self._back_on_the_10th()
        with self.assertRaises(UserError) as caught:
            self._entry(self._batch(SEP, self.friday), quantity=4.0)
        self.assertIn('3 Friday', str(caught.exception))

    def test_raising_the_count_past_them_is_refused(self):
        self._back_on_the_10th()
        entry = self._entry(self._batch(SEP, self.friday), quantity=2.0)
        with self.assertRaises(UserError):
            entry.with_user(self.officer).write({'quantity': 4.0})

    def test_the_register_holds_a_count_past_them(self):
        """A row typed before the rule still has to stay out of the run."""
        self._back_on_the_10th()
        batch = self._batch(SEP, self.friday)
        entry = self._entry(batch, quantity=3.0)
        entry.sudo().write({'quantity': 5.0})
        self.assertEqual(batch._held_entries(), entry)
