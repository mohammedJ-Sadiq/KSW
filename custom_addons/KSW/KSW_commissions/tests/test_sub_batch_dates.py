"""Send Early… — one component, some employees, some days, one button.

Opened from inside a component's batch. The supervisor picks the days and
the employees and presses Send to GM: the sub-batch is made, Driver Trips
is imported for the picked drivers and those days only, and it goes to the
General Manager. Nothing else to press, nothing to do in order.

A sub-batch belongs to one component. The earlier cross-component version
offered a Location Allowance sub-batch the drivers the trips import had
just filled, which is what these tests now pin down.

The batch's own import afterwards fills only the days no sub-batch covered,
one row per uncovered stretch; the free allowance and the tier bands are
cut to the days a row covers. Anything that still pays an employee twice
for the same thing is flagged, never blocked.
"""
from datetime import date
from unittest.mock import patch

from odoo.exceptions import UserError, ValidationError

from .test_sub_batch import SubBatchCommon


class SendEarlyCommon(SubBatchCommon):

    def setUp(self):
        super().setUp()
        self.trips = self.env.ref('KSW_commissions.pay_component_driver_trips')
        self.env['ksw.site']._trip_settings().sudo() \
            .required_trips_full_month = 62
        self.driver_1 = self._driver('Trip Driver One', 'DRIVER ONE')
        self.driver_2 = self._driver('Trip Driver Two', 'DRIVER TWO')
        self.driver_3 = self._driver('Trip Driver Three', 'DRIVER THREE')
        self.trips_batch = self._batch(
            self.sup_a, self.dept_a, component=self.trips)
        # Mutable, so built here and not in setUpClass.
        self.bas_calls = []
        self.bas_empty = False

    def _driver(self, name, cost_centre):
        emp = self._employee(name, self.dept_a, 5000.0)
        emp.sudo().x_bas_driver_cost_center = cost_centre
        return emp

    def _patched(self):
        """BAS stub: 30 loads per driver per 16 days, scaled to the days
        asked — so the windows are really exercised."""
        test = self

        def fake(batch, date_from, date_to):
            test.bas_calls.append((date_from, date_to))
            if test.bas_empty:
                return {}
            loads = round(30 * (date_to - date_from).days / 16.0)
            return {
                key: {'loads': loads, 'mult': float(loads), 'missing': 0,
                      'new_basis': loads, 'equip': 'T1', 'raw_cc': key}
                for key in ('driver one', 'driver two', 'driver three')
            }
        return patch.object(
            type(self.env['ksw.pay.batch']), '_bas_fetch_orood',
            autospec=True, side_effect=fake)

    def _dialog(self, batch, date_from=None, date_to=None):
        """Press Send Early… the way the button does."""
        action = batch.with_user(self.sup_a).action_send_early()
        self.assertEqual(action['res_model'], 'ksw.pay.send.early.wizard')
        wizard = self.env['ksw.pay.send.early.wizard'].with_user(
            self.sup_a).browse(action['res_id'])
        if date_from:
            wizard.date_from = date_from
        if date_to:
            wizard.date_to = date_to
        return wizard

    def _send(self, batch, employees, date_from=None, date_to=None):
        wizard = self._dialog(batch, date_from, date_to)
        wizard.employee_ids = [(6, 0, employees.ids)]
        with self._patched():
            action = wizard.action_send()
        sub = self.env['ksw.pay.sub.batch'].browse(
            action['params']['next']['res_id'])
        return sub

    def _import_batch(self, batch=None):
        with self._patched():
            return (batch or self.trips_batch).with_user(
                self.sup_a).action_import()

    def _rows(self, employee, batch=None):
        return (batch or self.trips_batch).sudo().entry_ids.filtered(
            lambda e: e.employee_id == employee)


class TestSendEarly(SendEarlyCommon):

    def test_01_dialog_defaults_to_the_whole_of_a_future_month(self):
        wizard = self._dialog(self.ot)
        self.assertEqual(wizard.date_from, date(2029, 3, 1))
        self.assertEqual(wizard.date_to, date(2029, 3, 31))

    def test_02_one_button_sends_only_rows_inside_the_days(self):
        late = self._entry(self.ot, self.emp_a, user=self.sup_a,
                           date='2029-03-20')
        sub = self._send(self.ot, self.emp_a, '2029-03-01', '2029-03-12')
        self.assertEqual(sub.state, 'submitted')
        self.assertEqual(sub.entry_ids, self.ot_a)
        self.assertEqual(self.ot_a.state, 'submitted')
        self.assertFalse(late.x_sub_batch_id)
        # Another component is not touched.
        self.assertFalse(self.meal_a.x_sub_batch_id)
        self.assertEqual(self.ot.state, 'draft')

    def test_03_the_picker_offers_only_this_components_people(self):
        wizard = self._dialog(self.meals_batch)
        self.assertEqual(wizard.allowed_employee_ids, self.emp_a)
        self.assertNotIn(self.driver_1, wizard.allowed_employee_ids)
        # Days with nothing typed in them offer nobody.
        wizard.write({'date_from': '2029-03-02', 'date_to': '2029-03-05'})
        ot = self._dialog(self.ot, '2029-03-02', '2029-03-05')
        self.assertFalse(ot.allowed_employee_ids)

    def test_04_one_employee_early_in_two_components_at_once(self):
        first = self._send(self.ot, self.emp_a)
        second = self._send(self.meals_batch, self.emp_a)
        self.assertEqual(first.component_id, self.component)
        self.assertEqual(second.component_id, self.meals)
        self.assertEqual(second.entry_ids, self.meal_a)

    def test_05_days_outside_the_month_leave_nothing_behind(self):
        before = self.env['ksw.pay.sub.batch'].sudo().search_count([])
        with self.assertRaises(ValidationError):
            self._send(self.ot, self.emp_a, '2029-03-10', '2029-04-02')
        self.assertEqual(
            self.env['ksw.pay.sub.batch'].sudo().search_count([]), before)

    def test_06_again_in_the_same_month_after_approval(self):
        first = self._send(self.ot, self.emp_a, '2029-03-01', '2029-03-03')
        first.with_user(self.gm).action_approve()
        self._entry(self.ot, self.emp_a, user=self.sup_a, date='2029-03-06')
        second = self._send(self.ot, self.emp_a, '2029-03-04', '2029-03-07')
        self.assertEqual(second.state, 'submitted')
        self.assertEqual(first.state, 'approved')

    def test_07_no_sub_batch_without_a_component_batch(self):
        with self.assertRaises(UserError):
            self.env['ksw.pay.sub.batch'].with_user(self.sup_a).create({
                'submission_id': self.submission.id,
                'employee_ids': [(6, 0, self.emp_a.ids)],
            })

    def test_08_another_departments_supervisor_cannot_send(self):
        with self.assertRaises(UserError):
            self.ot.with_user(self.sup_b).action_send_early()


class TestOverlapWarning(SendEarlyCommon):

    def test_10_same_allowance_early_and_again_in_the_batch(self):
        sub = self._send(self.meals_batch, self.emp_a)
        again = self._entry(self.meals_batch, self.emp_a, user=self.sup_a)
        self.assertIn(sub.name, again.x_overlap_note)
        self.assertIn('the batch itself', self.meal_a.x_overlap_note)
        self.assertEqual(self.meals_batch.x_overlap_count, 2)
        self.assertEqual(sub.x_overlap_count, 1)

    def test_11_overtime_on_other_days_is_not_a_double(self):
        sub = self._send(self.ot, self.emp_a, '2029-03-01', '2029-03-03')
        other_day = self._entry(self.ot, self.emp_a, user=self.sup_a,
                                date='2029-03-20')
        self.assertFalse(other_day.x_overlap_note)
        same_day = self._entry(self.ot, self.emp_a, user=self.sup_a,
                               date='2029-03-01')
        self.assertIn(sub.name, same_day.x_overlap_note)

    def test_12_a_warning_never_blocks(self):
        sub = self._send(self.meals_batch, self.emp_a)
        sub.with_user(self.gm).action_approve()
        self._entry(self.meals_batch, self.emp_a, user=self.sup_a)
        second = self._send(self.meals_batch, self.emp_a)
        self.assertEqual(second.state, 'submitted')
        self.assertTrue(second.x_overlap_count)


class TestSendEarlyTrips(SendEarlyCommon):

    def test_20_picked_drivers_imported_for_the_days_and_sent(self):
        sub = self._send(self.trips_batch, self.driver_1 | self.driver_2,
                         '2029-03-16', '2029-03-31')
        self.assertEqual(sub.state, 'submitted')
        rows = self.trips_batch.sudo().entry_ids
        self.assertEqual(rows.employee_id, self.driver_1 | self.driver_2)
        self.assertEqual(self.bas_calls,
                         [(date(2029, 3, 16), date(2029, 4, 1))])
        for row in rows:
            self.assertEqual((row.x_window_from, row.x_window_to),
                             (date(2029, 3, 16), date(2029, 3, 31)))
            self.assertEqual(row.x_sub_batch_id, sub)
            self.assertEqual(row.state, 'submitted')
        self.assertEqual(sub.sudo().employee_ids,
                         self.driver_1 | self.driver_2)

    def test_21_allowance_and_bands_are_cut_to_the_days(self):
        self._send(self.trips_batch, self.driver_1,
                   '2029-03-16', '2029-03-31')
        row = self._rows(self.driver_1)
        # 16 of 31 days, nothing recorded: 62 × 16/31 = 32 required.
        self.assertEqual(row.threshold_qty, 32.0)
        self.assertAlmostEqual(row._band_scale(), 16 / 31.0)
        _rate, cut = self.trips._resolve_tiered(
            row.quantity, threshold=row.threshold_qty,
            band_scale=row._band_scale())
        self.assertAlmostEqual(row.amount, cut, places=2)

    def test_22_batch_import_fills_only_the_uncovered_days(self):
        self._send(self.trips_batch, self.driver_1,
                   '2029-03-16', '2029-03-31')
        self.bas_calls.clear()
        self._import_batch()
        mine = self._rows(self.driver_1)
        self.assertEqual(len(mine), 2)
        rest = mine.filtered(lambda e: not e.x_sub_batch_id)
        self.assertEqual((rest.x_window_from, rest.x_window_to),
                         (date(2029, 3, 1), date(2029, 3, 15)))
        self.assertFalse(mine.filtered('x_overlap_note'))
        whole = self._rows(self.driver_2)
        self.assertEqual((whole.x_window_from, whole.x_window_to),
                         (date(2029, 3, 1), date(2029, 3, 31)))
        self.assertIn((date(2029, 3, 1), date(2029, 3, 16)), self.bas_calls)

    def test_23_an_earlier_whole_month_row_is_trimmed_not_doubled(self):
        self._import_batch()
        whole = self._rows(self.driver_1)
        self._send(self.trips_batch, self.driver_1,
                   '2029-03-16', '2029-03-31')
        self.assertTrue(whole.x_overlap_note)
        self._import_batch()
        mine = self._rows(self.driver_1)
        self.assertEqual(len(mine), 2)
        rest = mine.filtered(lambda e: not e.x_sub_batch_id)
        self.assertEqual(rest.x_window_to, date(2029, 3, 15))
        self.assertFalse(mine.filtered('x_overlap_note'))

    def test_24_everything_covered_means_nothing_to_import(self):
        self._send(self.trips_batch, self.driver_1)
        self._import_batch()
        self.assertEqual(len(self._rows(self.driver_1)), 1)
        self.assertIn('covered_by_sub_batch',
                      self.trips_batch.skip_line_ids.mapped('line_type'))

    def test_25_a_driver_outside_his_reach_is_refused(self):
        outsider = self._employee('Other Dept Driver', self.dept_b, 4000.0)
        outsider.sudo().x_bas_driver_cost_center = 'DRIVER ONE'
        wizard = self._dialog(self.trips_batch)
        self.assertNotIn(outsider, wizard.allowed_employee_ids)
        wizard.sudo().employee_ids = [(6, 0, outsider.ids)]
        with self.assertRaises(UserError), self._patched():
            wizard.action_send()

    def test_26_the_picker_offers_drivers_only(self):
        wizard = self._dialog(self.trips_batch)
        self.assertIn(self.driver_1, wizard.allowed_employee_ids)
        self.assertNotIn(self.emp_a2, wizard.allowed_employee_ids)

    def test_27_nothing_in_bas_sends_nothing_and_leaves_nothing(self):
        self.bas_empty = True
        before = self.env['ksw.pay.sub.batch'].sudo().search_count([])
        with self.assertRaises(UserError):
            self._send(self.trips_batch, self.driver_1)
        self.assertEqual(
            self.env['ksw.pay.sub.batch'].sudo().search_count([]), before)
        self.assertFalse(self.trips_batch.entry_ids)

    def test_28_a_returned_trips_sub_batch_imports_again(self):
        sub = self._send(self.trips_batch, self.driver_1,
                         '2029-03-16', '2029-03-31')
        self._review(sub, 'review', self.driver_1, reason='BAS was wrong')
        self.assertEqual(sub.state, 'returned')
        self.assertTrue(sub.with_user(self.sup_a).x_can_reimport)
        with self._patched():
            sub.with_user(self.sup_a).action_reimport()
        self.assertEqual(len(self._rows(self.driver_1)), 1)
        sub.with_user(self.sup_a).action_submit()
        self.assertEqual(sub.state, 'submitted')
