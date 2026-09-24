"""Importing driver trips when not every driver is in BAS.

The importer used to write a zero-quantity entry for any driver it could
not find in BAS. ``ksw.pay.entry._check_quantity`` rejects that, so the
ValidationError rolled back the *whole* import: one driver with no BAS
cost centre cost every other driver in the batch his trips, and the error
named nobody. These tests pin the behaviour that replaced it — skip the
driver, import the rest, name who was skipped.

Since 19.0.4.3.0 the drivers come from the batch's **department** rather
than from a work site named on the batch, so the fixture puts them in one.
"""
from datetime import date
from unittest.mock import patch

from odoo.tests.common import TransactionCase


class BasTripsImportCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.period = '2028-07-01'
        cls.dept = env['hr.department'].create({'name': 'BAS Drivers'})
        # The required-trips base is no longer a property of a place. It
        # lives on the one seeded settings record, for every location.
        cls.trip_settings = env['ksw.site']._trip_settings()
        cls.trip_settings.sudo().required_trips_full_month = 50
        cls.c_trips = env.ref('KSW_commissions.pay_component_driver_trips')

        # Matched: cost centre set, and BAS has loads for it.
        cls.driver_ok = cls._driver('Driver Matched', 'WAHAB JAN1387')
        # Cost centre set, but BAS returns nothing for it this month.
        cls.driver_no_data = cls._driver('Driver No Data', 'GHOST DRIVER999')
        # Never mapped at all.
        cls.driver_unmapped = cls._driver('Driver Unmapped', False)

        # `mult` is now Σ «رد الفاتورة» (STR10.TAXES_5) — the weighting as
        # invoiced — and `missing` counts loads BAS left unweighted.
        cls.bas_rows = {
            'wahab jan1387': {
                'loads': 118, 'mult': 161.23, 'missing': 0,
                'new_basis': 118,
                'equip': 'T166', 'raw_cc': 'WAHAB JAN1387',
            },
        }

    @classmethod
    def _driver(cls, name, cost_center):
        return cls.env['hr.employee'].sudo().create({
            'name': name,
            'department_id': cls.dept.id,
            'x_bas_driver_cost_center': cost_center,
        })

    def _batch(self):
        return self.env['ksw.pay.batch'].sudo().create({
            'component_id': self.c_trips.id,
            'period': self.period,
            'department_id': self.dept.id,
        })

    def _import(self, batch, rows=None):
        """Run the importer with BAS stubbed out."""
        rows = self.bas_rows if rows is None else rows
        with patch.object(
            type(batch), '_bas_fetch_orood', return_value=rows
        ):
            return batch._import_bas_trips()

    @staticmethod
    def _message(result):
        return result['params']['message']


class TestBasTripsImport(BasTripsImportCommon):

    def test_01_unmapped_driver_does_not_roll_back_the_import(self):
        """The matched driver is imported even though two others are not."""
        batch = self._batch()
        result = self._import(batch)

        self.assertEqual(len(batch.entry_ids), 1)
        entry = batch.entry_ids
        self.assertEqual(entry.employee_id, self.driver_ok)
        self.assertAlmostEqual(entry.quantity, 161.23, places=2)
        self.assertAlmostEqual(entry.quantity_ref, 118.0, places=2)
        self.assertIn('1 driver(s) filled from BAS', self._message(result))

    def test_02_skipped_drivers_are_named(self):
        """The summary says who was skipped, and why — the old error did not."""
        batch = self._batch()
        message = self._message(self._import(batch))

        self.assertIn('no BAS cost centre set', message)
        self.assertIn('Driver Unmapped', message)
        self.assertIn('had no trips in BAS this month', message)
        self.assertIn('Driver No Data', message)
        # The matched driver is not reported as SKIPPED. (He is named
        # further down under "no attendance recorded" — this fixture gives
        # nobody a sheet — which is a different report, not a skip.)
        skips = message.split('no attendance recorded')[0]
        self.assertNotIn('Driver Matched', skips)

    def test_03_zero_quantity_entry_is_never_created(self):
        """No entry at all for a skipped driver, rather than a 0 one."""
        batch = self._batch()
        self._import(batch)

        self.assertNotIn(
            self.driver_no_data, batch.entry_ids.mapped('employee_id'))
        self.assertNotIn(
            self.driver_unmapped, batch.entry_ids.mapped('employee_id'))
        self.assertFalse(batch.entry_ids.filtered(lambda e: e.quantity <= 0))

    def test_04_no_matches_at_all_reports_instead_of_raising(self):
        """An empty BAS month is a message, not a failed button."""
        batch = self._batch()
        message = self._message(self._import(batch, rows={}))

        self.assertFalse(batch.entry_ids)
        self.assertIn('0 driver(s) filled from BAS', message)

    def test_05_existing_line_of_a_skipped_driver_is_left_alone(self):
        """A figure already in the batch is not silently destroyed."""
        batch = self._batch()
        existing = self.env['ksw.pay.entry'].sudo().create({
            'batch_id': batch.id,
            'employee_id': self.driver_no_data.id,
            'quantity': 42.0,
        })
        message = self._message(self._import(batch))

        self.assertTrue(existing.exists())
        self.assertAlmostEqual(existing.quantity, 42.0, places=2)
        self.assertIn('left unchanged', message)
        self.assertIn('Driver No Data', message)

    def test_06_rerun_updates_the_matched_line_in_place(self):
        """Importing twice does not duplicate the matched driver."""
        batch = self._batch()
        self._import(batch)
        first = batch.entry_ids
        self._import(batch, rows={
            'wahab jan1387': {
                'loads': 120, 'mult': 170.0,
                'equip': 'T166', 'raw_cc': 'WAHAB JAN1387',
            },
        })

        self.assertEqual(len(batch.entry_ids), 1)
        self.assertEqual(batch.entry_ids, first)
        self.assertAlmostEqual(batch.entry_ids.quantity, 170.0, places=2)

    def test_07_import_refreshes_the_form(self):
        """The toast chains into a reload, or the Entries tab stays stale.

        A button that returns an action does not get the web client's
        automatic record reload, so the imported lines would sit unseen in
        the database until the user refreshed the page by hand.
        """
        batch = self._batch()
        result = self._import(batch)

        self.assertEqual(
            result['params'].get('next'),
            {'type': 'ir.actions.client', 'tag': 'soft_reload'})

    def test_08_threshold_is_prorated_to_the_default_allowance(self):
        """No attendance sheet -> the full-month allowance, not zero.

        And it comes from the settings record, which is the point of the
        record: the batch names no site to read it from.
        """
        batch = self._batch()
        self._import(batch)

        self.assertAlmostEqual(batch.entry_ids.threshold_qty, 50.0, places=2)


class TestImportSkipsHeldDrivers(BasTripsImportCommon):
    """Every blocking kind of hold must skip, not just 'full'.

    The import tested `hold.kind == 'full'`, so when 'settled_later' was
    added the driver fell past the skip and into `windows[None]` —
    KeyError on the live Import button (KSWCO, 21 Sep 2026). One test per
    kind, so the next one added fails here instead of in production.
    """

    def _leave(self, date_from, date_to, return_date=None):
        leave_type = self.env['hr.leave.type'].sudo().create({
            'name': 'Trips Import Hold Test',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'request_unit': 'day',
        })
        leave = self.env['hr.leave'].sudo().with_context(
            tracking_disable=True, mail_create_nosubscribe=True,
        ).create({
            'employee_id': self.driver_ok.id,
            'holiday_status_id': leave_type.id,
            'request_date_from': date_from,
            'request_date_to': date_to,
        })
        vals = {'state': 'validate', 'x_return_state': 'on_vacation'}
        if return_date:
            vals.update(x_return_date=return_date,
                        x_return_state='hr_confirmed')
        leave.sudo().write(vals)
        return leave

    def test_a_driver_away_during_the_month_is_skipped(self):
        self._leave(date(2028, 7, 10), date(2028, 9, 30))
        batch = self._batch()
        message = self._message(self._import(batch))
        self.assertNotIn(self.driver_ok, batch.entry_ids.employee_id)
        self.assertIn('were on vacation this month', message)

    def test_the_vacation_skip_is_logged_with_the_leave_date(self):
        self._leave(date(2028, 7, 10), date(2028, 9, 30))
        batch = self._batch()
        self._import(batch)
        line = batch.skip_line_ids.filtered(
            lambda l: l.employee_id == self.driver_ok)
        self.assertEqual(line.line_type, 'on_vacation')
        self.assertEqual(line.outcome, 'skipped')
        self.assertIn('2028-07-10', line.reason,
                      'The reason must name the day he left.')

    def test_a_settled_later_skip_says_the_month_was_still_owed(self):
        self._leave(date(2028, 9, 3), date(2028, 12, 1))
        batch = self._batch()
        self._import(batch)
        line = batch.skip_line_ids.filtered(
            lambda l: l.employee_id == self.driver_ok)
        self.assertEqual(line.line_type, 'on_vacation')
        self.assertIn('still owed', line.reason)

    def test_a_stale_line_left_behind_is_called_out(self):
        """He was imported before the leave existed; that line still sits
        on the batch and will not be paid."""
        batch = self._batch()
        self._import(batch)
        self.assertIn(self.driver_ok, batch.entry_ids.employee_id)
        self._leave(date(2028, 7, 10), date(2028, 9, 30))
        self._import(batch)
        line = batch.skip_line_ids.filtered(
            lambda l: l.employee_id == self.driver_ok)
        self.assertIn('delete it', line.reason)

    def test_a_driver_who_left_after_the_month_is_skipped(self):
        """'settled_later' — the month was still owed when he left.

        This is the one that raised KeyError: None in production.
        """
        self._leave(date(2028, 9, 3), date(2028, 12, 1))
        batch = self._batch()
        message = self._message(self._import(batch))   # must not raise
        self.assertNotIn(self.driver_ok, batch.entry_ids.employee_id)
        self.assertIn('were on vacation this month', message)

    def test_a_driver_back_mid_month_is_imported_from_his_return_date(self):
        """'partial' — the only kind with a window to fetch."""
        self._leave(date(2028, 6, 1), date(2028, 7, 16),
                    return_date=date(2028, 7, 17))
        batch = self._batch()
        calls = []
        real = type(batch)._bas_fetch_orood

        def spy(self_, date_from, date_to):
            calls.append(date_from)
            return self.bas_rows

        type(batch)._bas_fetch_orood = spy
        try:
            message = self._message(batch._import_bas_trips())
        finally:
            type(batch)._bas_fetch_orood = real
        self.assertIn(date(2028, 7, 17), calls,
                      'BAS must be re-queried from the return date.')
        self.assertIn(self.driver_ok, batch.entry_ids.employee_id)
        self.assertIn('came back from vacation during the month', message)


class TestRequiredTripsFollowTheAttendanceSheet(BasTripsImportCommon):
    """The free allowance is pro-rated to the days actually recorded.

    A driver the supervisor marked present for a handful of days is asked
    to earn against those days, not against a full month. The figure comes
    straight off ``ksw.attendance.sheet.total_attended``.

    The tests assert the **rule** against the sheet's own total rather than
    a hardcoded number, because `total_attended` is not simply "the days
    the supervisor ticked": the weekly rest days are computed (and refused
    to a direct write), and one stays paid or is forfeited depending on
    whether the days around it were attended. Hardcoding would pin the
    off-day policy into a test that is not about the off-day policy.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A 31-day month, which is where the old fixed /30 divisor showed.
        cls.period = '2028-08-01'
        cls.driver_ok.sudo().write({'x_is_attendance_sheet': True})

    def _sheet(self, attended_workdays, state='draft'):
        """A sheet for the batch month with `attended_workdays` marked.

        The lines are the generator's own — `create` calls
        `action_generate_lines`, so a second set added by hand would
        double every day. Only **workday** lines are written: an off day
        is computed and `attendance_sheet_line.write` refuses it outright.
        """
        sheet = self.env['ksw.attendance.sheet'].sudo().create({
            'employee_id': self.driver_ok.id,
            'month': '8', 'year': 2028,
        })
        workdays = sheet.line_ids.filtered('is_workday').sorted('date')
        workdays[:attended_workdays].write({'is_attended': True})
        workdays[attended_workdays:].write({'is_attended': False})
        sheet.invalidate_recordset(['total_attended'])
        if state != 'draft':
            sheet.write({'state': state})
        return sheet

    @property
    def _base(self):
        return self.trip_settings.required_trips_full_month

    def _expected(self, sheet):
        """What the rule says, given what the sheet actually says."""
        return float(min(
            self._base, round(self._base * sheet.total_attended / 31.0)))

    def _required(self, batch):
        return batch.entry_ids.filtered(
            lambda e: e.employee_id == self.driver_ok).threshold_qty

    # ------------------------------------------------------------------
    def test_a_partly_marked_month_pro_rates_the_requirement(self):
        """Four workdays marked: nothing like a full month is demanded."""
        sheet = self._sheet(4)
        batch = self._batch()
        self._import(batch)

        self.assertLess(sheet.total_attended, 31,
                        'Fixture: the month must not be fully attended.')
        self.assertAlmostEqual(
            self._required(batch), self._expected(sheet), places=2)
        self.assertLess(
            self._required(batch), self._base,
            'A part month must ask for less than a full month.')

    def test_a_full_month_never_asks_for_more_than_the_base(self):
        """The old fixed /30 divisor asked 52 of a 31-day month's base 50."""
        sheet = self._sheet(31)
        batch = self._batch()
        self._import(batch)

        self.assertEqual(sheet.total_attended, 31)
        self.assertAlmostEqual(self._required(batch), self._base, places=2)

    def test_the_worked_days_are_written_on_the_entry(self):
        """The figure has to justify itself where it is read."""
        sheet = self._sheet(4)
        batch = self._batch()
        self._import(batch)
        entry = batch.entry_ids.filtered(
            lambda e: e.employee_id == self.driver_ok)
        self.assertIn('Worked days: %s' % sheet.total_attended, entry.details)

    def test_a_draft_sheet_is_read_too(self):
        """The month is prepared before the sheets are signed off."""
        sheet = self._sheet(4, state='draft')
        batch = self._batch()
        self._import(batch)
        self.assertEqual(sheet.state, 'draft')
        self.assertAlmostEqual(
            self._required(batch), self._expected(sheet), places=2)

    def test_no_attendance_anywhere_charges_the_full_base_and_says_so(self):
        batch = self._batch()
        message = self._message(self._import(batch))
        self.assertAlmostEqual(self._required(batch), self._base, places=2)
        self.assertIn('no attendance recorded', message)
        self.assertIn('Driver Matched', message)

    def test_a_biometric_driver_is_counted_from_his_punches(self):
        """One driver in ten has no sheet — he must not pay for that."""
        self.driver_ok.sudo().write({'x_is_attendance_sheet': False})
        Attendance = self.env['hr.attendance'].sudo()
        for day in (1, 2, 3, 4):
            Attendance.create({
                'employee_id': self.driver_ok.id,
                'check_in': '2028-08-%02d 05:00:00' % day,
                'check_out': '2028-08-%02d 13:00:00' % day,
            })
        batch = self._batch()
        message = self._message(self._import(batch))

        expected = float(min(self._base, round(self._base * 4 / 31.0)))
        self.assertAlmostEqual(self._required(batch), expected, places=2)
        self.assertNotIn('no attendance recorded', message)


class TestImportLog(BasTripsImportCommon):
    """Every driver the import did not fill gets a row saying why.

    A toast says it once and is gone. The supervisor an hour later has no
    way to answer 'why is the department four drivers short?' except by
    pressing Import again — which is exactly what the payslip batch's
    skip log already exists to prevent.
    """

    def _log(self, batch, employee):
        return batch.skip_line_ids.filtered(
            lambda l: l.employee_id == employee)

    def test_an_unmapped_driver_is_logged_with_the_fix(self):
        batch = self._batch()
        self._import(batch)
        line = self._log(batch, self.driver_unmapped)
        self.assertEqual(len(line), 1)
        self.assertEqual(line.line_type, 'no_cost_centre')
        self.assertEqual(line.outcome, 'skipped')
        self.assertIn('Cost Center', line.reason)

    def test_a_driver_with_no_bas_data_is_logged_with_his_cost_centre(self):
        batch = self._batch()
        self._import(batch)
        line = self._log(batch, self.driver_no_data)
        self.assertEqual(line.line_type, 'no_data')
        self.assertEqual(line.outcome, 'skipped')
        self.assertIn('GHOST DRIVER999', line.reason,
                      'The reason must name the key that failed to match.')

    def test_a_filled_driver_is_not_logged(self):
        batch = self._batch()
        self._import(batch)
        self.assertFalse(
            self._log(batch, self.driver_ok).filtered(
                lambda l: l.outcome == 'skipped'))

    def test_the_counts_split_skipped_from_review(self):
        batch = self._batch()
        self._import(batch)
        self.assertEqual(
            batch.skipped_count,
            len(batch.skip_line_ids.filtered(
                lambda l: l.outcome == 'skipped')))
        self.assertEqual(
            batch.review_count,
            len(batch.skip_line_ids.filtered(
                lambda l: l.outcome == 'warning')))
        self.assertTrue(batch.skipped_count)

    def test_a_second_import_rebuilds_the_log_rather_than_appending(self):
        """A stale row whose cause was fixed is worse than none."""
        batch = self._batch()
        self._import(batch)
        first = len(batch.skip_line_ids)
        self.assertTrue(first)
        self._import(batch)
        self.assertEqual(len(batch.skip_line_ids), first)

    def test_clearing_the_log_empties_it(self):
        batch = self._batch()
        self._import(batch)
        self.assertTrue(batch.skip_line_ids)
        batch.action_clear_skip_log()
        self.assertFalse(batch.skip_line_ids)

    def test_the_log_dies_with_its_batch(self):
        batch = self._batch()
        self._import(batch)
        ids = batch.skip_line_ids.ids
        self.assertTrue(ids)
        batch.sudo().unlink()
        self.assertFalse(
            self.env['ksw.pay.batch.skip.line'].sudo().browse(ids).exists())


class TestInvoiceFactorSource(BasTripsImportCommon):
    """The weighting comes from the invoice line, not the customer.

    «الرد المضاعف» read `cod10.FACTORE`, the destination's *current*
    multiplier, so changing a customer's ratio silently rewrote every
    month ever imported from it — including months already paid. It now
    reads «رد الفاتورة» (`STR10.TAXES_5`), frozen when the invoice was
    issued, so a ratio change applies from the day it is set.

    The queries themselves are stubbed here; what these pin down is the
    handling of a load BAS never weighted, which is not the same thing as
    a load worth nothing. BAS stopped writing the column on 6 Sep 2026.
    """

    def _rows(self, **kw):
        row = dict(self.bas_rows['wahab jan1387'])
        row.update(kw)
        return {'wahab jan1387': row}

    def _entry(self, batch):
        return batch.entry_ids.filtered(
            lambda e: e.employee_id == self.driver_ok)

    def _factor_log(self, batch):
        """Only the رد الفاتورة rows. This fixture gives nobody an
        attendance sheet, so every driver also collects a
        'no_attendance' row that has nothing to do with these tests."""
        return batch.skip_line_ids.filtered(
            lambda l: l.employee_id == self.driver_ok
            and l.line_type in ('no_invoice_factor', 'part_invoice_factor'))

    def test_the_quantity_is_the_invoiced_weighting(self):
        batch = self._batch()
        self._import(batch, rows=self._rows(mult=161.23))
        self.assertAlmostEqual(self._entry(batch).quantity, 161.23, places=2)
        self.assertAlmostEqual(
            self._entry(batch).quantity_ref, 118.0, places=2,
            msg='عدد الردود stays the raw load count.')

    def test_a_driver_with_no_factor_at_all_is_skipped_not_zeroed(self):
        """Zero would say 'he did less'. He did the loads; BAS did
        not write the weighting.
        """
        batch = self._batch()
        message = self._message(
            self._import(batch, rows=self._rows(mult=0.0, missing=118)))
        self.assertFalse(self._entry(batch),
                         'No zero-quantity entry may be created.')
        self.assertIn('رد الفاتورة', message)
        line = self._factor_log(batch)
        self.assertEqual(line.line_type, 'no_invoice_factor')
        self.assertEqual(line.outcome, 'skipped')
        self.assertIn('118', line.reason,
                      'The reason must say how many loads he really did.')

    def test_partly_weighted_is_imported_with_a_warning(self):
        batch = self._batch()
        self._import(batch, rows=self._rows(mult=100.0, missing=18))
        self.assertAlmostEqual(self._entry(batch).quantity, 100.0, places=2)
        line = self._factor_log(batch)
        self.assertEqual(line.line_type, 'part_invoice_factor')
        self.assertEqual(line.outcome, 'warning')
        self.assertIn('18', line.reason)
        self.assertIn('100', self._entry(batch).details,
                      'The entry says how many loads were weighted.')

    def test_a_fully_weighted_driver_gets_no_log_row(self):
        batch = self._batch()
        self._import(batch, rows=self._rows(mult=161.23, missing=0))
        self.assertFalse(self._factor_log(batch))

    def test_the_query_carries_both_bases_and_splits_on_the_cutover(self):
        """Guards the actual SQL. Both bases must be present, and the
        choice must be made on the document's own date."""
        import inspect
        from odoo.addons.KSW_commissions.models import ksw_pay_import_bas
        src = inspect.getsource(
            ksw_pay_import_bas.KswPayBatchBasImport._bas_fetch_orood)
        self.assertIn('INVOICE_FACTOR_COLUMN', src)
        self.assertIn('FACTORE', src,
                      'The old basis still applies before the cutover.')
        self.assertIn('h.FDATE >= %s', src,
                      'The split must be per line, on the invoice date.')
        self.assertEqual(
            ksw_pay_import_bas.INVOICE_FACTOR_COLUMN, 'TAXES_5',
            'Confirmed against «الحركة التجارية للأصناف», doc 172/9026417.')

    def test_the_cutover_defaults_to_the_changeover_date(self):
        from datetime import date as _date
        self.assertEqual(self._batch()._invoice_factor_cutover(),
                         _date(2026, 9, 6))

    def test_the_cutover_is_configurable(self):
        from datetime import date as _date
        self.env['ir.config_parameter'].sudo().set_param(
            'ksw_commissions.invoice_factor_from', '2026-01-01')
        self.assertEqual(self._batch()._invoice_factor_cutover(),
                         _date(2026, 1, 1))

    def test_an_unreadable_cutover_falls_back_instead_of_re_basing(self):
        """A broken setting must not silently re-base every month."""
        from datetime import date as _date
        self.env['ir.config_parameter'].sudo().set_param(
            'ksw_commissions.invoice_factor_from', 'not-a-date')
        self.assertEqual(self._batch()._invoice_factor_cutover(),
                         _date(2026, 9, 6))

    def test_august_is_costed_on_the_old_basis(self):
        """The batch period is before the cutover, so every load is
        weighted on «الرد المضاعف» and nothing can be 'missing'."""
        batch = self._batch()          # period 2028-07-01 in this fixture
        self._import(batch, rows=self._rows(mult=161.23, missing=0,
                                            new_basis=0))
        self.assertIn('الرد المضاعف', self._entry(batch).details)
        self.assertFalse(self._factor_log(batch))


class TestStrandedEntries(BasTripsImportCommon):
    """An entry the importer cannot reach must say so.

    `_allowed_employees` is the batch's scope, and the import loop walks
    only that. An entry for somebody outside it is passed over in
    silence and keeps whatever figure was last written — while still
    being paid. KSWCO, Aug 2026: four drivers lost their department and
    went stale for weeks; one of them earned 0.00 because his frozen
    figure sat below the trip threshold while his real one was above it.
    """

    def _strand(self):
        """Put an entry in the batch, then move the driver out of
        scope — the order the real thing happens in.
        """
        batch = self._batch()
        self._import(batch)
        entry = batch.entry_ids.filtered(
            lambda e: e.employee_id == self.driver_ok)
        self.assertTrue(entry, 'Fixture: he must be in the batch first.')
        self.driver_ok.sudo().write({'department_id': False})
        batch.invalidate_recordset()
        return batch, entry

    def test_a_stranded_entry_is_logged_and_left_alone(self):
        batch, entry = self._strand()
        before = entry.quantity
        self._import(batch)

        self.assertTrue(entry.exists(), 'The entry must not be removed.')
        self.assertEqual(entry.quantity, before,
                         'It cannot be refreshed, so it must not change.')
        line = batch.skip_line_ids.filtered(
            lambda l: l.employee_id == self.driver_ok
            and l.line_type == 'out_of_scope')
        self.assertEqual(len(line), 1)
        self.assertEqual(line.outcome, 'warning',
                         'The money is still paid — this is not a skip.')
        self.assertIn('department', line.reason,
                      'The reason must name the usual cause.')

    def test_the_summary_names_him(self):
        batch, _entry = self._strand()
        message = self._message(self._import(batch))
        self.assertIn('outside this batch', message)
        self.assertIn('Driver Matched', message)

    def test_an_in_scope_driver_gets_no_such_row(self):
        batch = self._batch()
        self._import(batch)
        self.assertFalse(batch.skip_line_ids.filtered(
            lambda l: l.line_type == 'out_of_scope'))

    def test_putting_the_department_back_refreshes_him(self):
        """The fix is HR data, and one import must be enough."""
        batch, entry = self._strand()
        self._import(batch)
        self.assertTrue(batch.skip_line_ids.filtered(
            lambda l: l.line_type == 'out_of_scope'))

        self.driver_ok.sudo().write({'department_id': self.dept.id})
        batch.invalidate_recordset()
        self._import(batch, rows={'wahab jan1387': dict(
            self.bas_rows['wahab jan1387'], mult=200.0, new_basis=118)})

        self.assertAlmostEqual(entry.quantity, 200.0, places=2)
        self.assertFalse(batch.skip_line_ids.filtered(
            lambda l: l.line_type == 'out_of_scope'))


class TestCostCentreAliases(BasTripsImportCommon):
    """One driver, two BAS spellings, one entry.

    BAS booked IRFAN ULLAH QASIM KHAN1483 under both an English and an
    Arabic rendering of the same name and number. His August arrived
    split 2 loads / 114, with nothing on either piece to say the other
    existed — the employee record matched the English one, so 114 loads
    were simply invisible. Correcting the figure by hand would not have
    survived the next import.
    """

    def _rows(self):
        return {
            'wahab jan1387': dict(self.bas_rows['wahab jan1387'],
                                  loads=10, mult=20.0, missing=0,
                                  new_basis=0),
            'wahab jan1387 arabic': {
                'loads': 100, 'mult': 150.0, 'missing': 0, 'new_basis': 0,
                'equip': 'T166', 'raw_cc': 'WAHAB JAN1387 ARABIC'},
        }

    def test_without_an_alias_only_the_main_spelling_counts(self):
        batch = self._batch()
        self._import(batch, rows=self._rows())
        e = batch.entry_ids.filtered(
            lambda x: x.employee_id == self.driver_ok)
        self.assertAlmostEqual(e.quantity, 20.0, places=2)
        self.assertAlmostEqual(e.quantity_ref, 10.0, places=2)

    def test_an_alias_adds_the_other_spelling(self):
        self.driver_ok.sudo().write(
            {'x_bas_driver_cost_center_alt': 'WAHAB JAN1387 ARABIC'})
        batch = self._batch()
        self._import(batch, rows=self._rows())
        e = batch.entry_ids.filtered(
            lambda x: x.employee_id == self.driver_ok)
        self.assertAlmostEqual(e.quantity, 170.0, places=2)
        self.assertAlmostEqual(e.quantity_ref, 110.0, places=2,
                               msg='Both load counts add up too.')

    def test_aliases_are_normalised_and_deduplicated(self):
        """Case, stray spaces and a repeat of the main one."""
        self.driver_ok.sudo().write({
            'x_bas_driver_cost_center_alt':
                '  wahab   jan1387 arabic , WAHAB JAN1387 ,'})
        batch = self._batch()
        self._import(batch, rows=self._rows())
        e = batch.entry_ids.filtered(
            lambda x: x.employee_id == self.driver_ok)
        self.assertAlmostEqual(
            e.quantity, 170.0, places=2,
            msg='The main spelling must not be counted twice.')

    def test_an_alias_that_matches_nothing_is_harmless(self):
        self.driver_ok.sudo().write(
            {'x_bas_driver_cost_center_alt': 'NOBODY AT ALL999'})
        batch = self._batch()
        self._import(batch, rows=self._rows())
        e = batch.entry_ids.filtered(
            lambda x: x.employee_id == self.driver_ok)
        self.assertAlmostEqual(e.quantity, 20.0, places=2)


class TestNameList(BasTripsImportCommon):

    def test_09_long_name_list_is_truncated_with_a_count(self):
        batch = self._batch()
        extras = self.env['hr.employee'].sudo().browse()
        for i in range(7):
            extras |= self._driver('Extra %s' % i, False)

        message = self._message(self._import(batch))
        self.assertIn('and 3 more', message)
