# -*- coding: utf-8 -*-
from calendar import monthrange
from datetime import datetime as dt, timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestAttendanceSheet(TransactionCase):
    """Tests for the KSW Attendance Sheet module."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # ── Work schedule: Sun-Thu (0=Mon..6=Sun) ──
        # In our setup: Sunday=6, Monday=0, Tuesday=1, Wednesday=2, Thursday=3
        # So work days = 0,1,2,3,6  (Mon,Tue,Wed,Thu,Sun)
        cls.calendar_group = cls.env['resource.calendar.group'].create({
            'name': 'Test Standard Group',
        })
        work_days = ['0', '1', '2', '3', '6']  # Mon-Thu + Sun
        for day in work_days:
            cls.env['resource.calendar.group.line'].create({
                'name': f'Work Day {day}',
                'calendar_group_id': cls.calendar_group.id,
                'dayofweek': day,
                'day_period': 'full_day',
                'hour_from': 8.0,
                'hour_to': 16.5,
            })
            # Add break
            cls.env['resource.calendar.group.line'].create({
                'name': f'Break Day {day}',
                'calendar_group_id': cls.calendar_group.id,
                'dayofweek': day,
                'day_period': 'break',
                'hour_from': 12.0,
                'hour_to': 13.0,
            })

        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Test Calendar',
            'tz': 'Asia/Riyadh',
            'calendar_group_ids': [(4, cls.calendar_group.id)],
        })

        cls.manager_user = cls.env['res.users'].create({
            'name': 'Test Manager',
            'login': 'test_manager_sheet',
            'group_ids': [
                (4, cls.env.ref('hr.group_hr_user').id),
                (4, cls.env.ref('KSW_attendance_sheet.group_attendance_sheet_manager').id),
            ],
        })
        cls.manager_employee = cls.env['hr.employee'].create({
            'name': 'Test Manager Employee',
            'user_id': cls.manager_user.id,
        })

        cls.employee = cls.env['hr.employee'].create({
            'name': 'Test Sheet Employee',
            'resource_calendar_id': cls.calendar.id,
            'parent_id': cls.manager_employee.id,
            'x_is_attendance_sheet': False,
        })

        # Use a fixed month/year for deterministic tests
        cls.test_year = 2026
        cls.test_month = '1'  # January 2026

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_sheet(self, employee=None, month=None, year=None):
        """Create a sheet with defaults from setUpClass."""
        return self.env['ksw.attendance.sheet'].create({
            'employee_id': (employee or self.employee).id,
            'month': month or self.test_month,
            'year': year or self.test_year,
        })

    def _count_workdays_in_month(self, year, month_int):
        """Count expected workdays (Mon-Thu + Sun) in a month."""
        num_days = monthrange(year, month_int)[1]
        work_weekdays = {0, 1, 2, 3, 6}  # Mon-Thu + Sun
        count = 0
        for day in range(1, num_days + 1):
            d = fields.Date.to_date(f'{year}-{month_int:02d}-{day:02d}')
            if d.weekday() in work_weekdays:
                count += 1
        return count

    # ------------------------------------------------------------------
    # Test: Sheet creation auto-generates lines
    # ------------------------------------------------------------------

    def test_create_auto_generates_lines(self):
        """Creating a sheet should auto-generate one line per calendar day."""
        sheet = self._create_sheet()
        num_days = monthrange(self.test_year, int(self.test_month))[1]
        self.assertEqual(
            len(sheet.line_ids), num_days,
            "Sheet should have one line per calendar day of the month.",
        )

    def test_lines_workday_flag(self):
        """Lines on work days should have is_workday=True; others False."""
        sheet = self._create_sheet()
        work_weekdays = {0, 1, 2, 3, 6}
        for line in sheet.line_ids:
            expected = line.date.weekday() in work_weekdays
            self.assertEqual(
                line.is_workday, expected,
                f"Day {line.date} (weekday={line.date.weekday()}) should "
                f"have is_workday={expected}.",
            )

    def test_lines_default_attended(self):
        """ALL lines should default to is_attended=True (including weekends)."""
        sheet = self._create_sheet()
        for line in sheet.line_ids:
            self.assertTrue(
                line.is_attended,
                f"Line {line.date} should default to attended.",
            )

    def test_lines_have_attendance_records(self):
        """ALL attended lines should have hr.attendance records on creation."""
        sheet = self._create_sheet()
        for line in sheet.line_ids:
            self.assertTrue(
                line.attendance_id,
                f"Line {line.date} should have an attendance record.",
            )
            self.assertTrue(line.attendance_id.x_is_auto_generated)

    def test_day_name_computed(self):
        """Each line should have its day name computed (e.g. 'Thursday')."""
        sheet = self._create_sheet()
        for line in sheet.line_ids:
            expected_name = line.date.strftime('%A')
            self.assertEqual(line.day_name, expected_name)

    # ------------------------------------------------------------------
    # Test: Totals computation
    # ------------------------------------------------------------------

    def test_totals_all_present(self):
        """With all days attended, total_absent should be 0."""
        sheet = self._create_sheet()
        num_days = monthrange(self.test_year, int(self.test_month))[1]
        self.assertEqual(sheet.total_days, num_days)
        self.assertEqual(sheet.total_attended, num_days)
        self.assertEqual(sheet.total_absent, 0)

    def test_totals_some_absent(self):
        """Marking some days absent should update totals."""
        sheet = self._create_sheet()
        # Mark first 3 lines as absent
        to_mark = sheet.line_ids[:3]
        to_mark.write({'is_attended': False})

        self.assertEqual(sheet.total_absent, 3)
        self.assertEqual(
            sheet.total_attended,
            sheet.total_days - 3,
        )

    # ------------------------------------------------------------------
    # Test: Mark all absent / present
    # ------------------------------------------------------------------

    def test_mark_all_absent(self):
        """action_mark_all_absent should set ALL lines to not attended."""
        sheet = self._create_sheet()
        sheet.action_mark_all_absent()

        for line in sheet.line_ids:
            self.assertFalse(line.is_attended)
        # All attendance records should be deleted
        for line in sheet.line_ids:
            self.assertFalse(line.attendance_id)

    def test_mark_all_present(self):
        """action_mark_all_present should set ALL lines to attended."""
        sheet = self._create_sheet()
        sheet.action_mark_all_absent()  # first mark all absent
        sheet.action_mark_all_present()

        for line in sheet.line_ids:
            self.assertTrue(line.is_attended)
        # All attendance records should be recreated
        for line in sheet.line_ids:
            self.assertTrue(line.attendance_id)

    # ------------------------------------------------------------------
    # Test: off-day (weekly rest day) pay derivation
    # ------------------------------------------------------------------

    def _line_on(self, sheet, day):
        """Return the sheet line for `day` of the sheet's month."""
        d = fields.Date.to_date(f'{sheet.year}-{int(sheet.month):02d}-{day:02d}')
        return sheet.line_ids.filtered(lambda l: l.date == d)

    def test_off_day_forfeited_when_both_neighbours_absent(self):
        """Mid-month rest day is forfeited only when both sides are absent."""
        # Jan 2026, schedule Sun-Thu: Fri 9th + Sat 10th are the off block,
        # bordered by Thu 8th and Sun 11th.
        sheet = self._create_sheet()
        self.assertFalse(self._line_on(sheet, 9).is_workday)
        self.assertFalse(self._line_on(sheet, 10).is_workday)

        (self._line_on(sheet, 8) | self._line_on(sheet, 11)).write(
            {'is_attended': False})

        self.assertFalse(self._line_on(sheet, 9).is_attended)
        self.assertFalse(self._line_on(sheet, 10).is_attended)

    def test_off_day_paid_when_one_neighbour_present(self):
        """Mid-month rest day stays paid when only one side is absent."""
        sheet = self._create_sheet()
        self._line_on(sheet, 8).write({'is_attended': False})

        self.assertTrue(self._line_on(sheet, 9).is_attended)
        self.assertTrue(self._line_on(sheet, 10).is_attended)

    def test_off_day_at_month_end_uses_only_known_neighbour(self):
        """A rest day on the last day(s) of the month has no 'day after'
        inside this sheet — the day before alone decides it."""
        # Jan 2026 ends Fri 30th + Sat 31st, bordered only by Thu 29th.
        sheet = self._create_sheet()
        last_day = monthrange(sheet.year, int(sheet.month))[1]
        self.assertEqual(last_day, 31)
        self.assertFalse(self._line_on(sheet, 30).is_workday)
        self.assertFalse(self._line_on(sheet, 31).is_workday)

        self._line_on(sheet, 29).write({'is_attended': False})

        self.assertFalse(
            self._line_on(sheet, 30).is_attended,
            "Month-end rest day must follow the only neighbour it has")
        self.assertFalse(self._line_on(sheet, 31).is_attended)

    def test_off_day_at_month_end_paid_when_previous_day_present(self):
        """The month-edge fallback must not forfeit a rest day when the
        one known neighbour was present."""
        sheet = self._create_sheet()
        # Absent earlier in the month, present on Thu 29th.
        self._line_on(sheet, 8).write({'is_attended': False})

        self.assertTrue(self._line_on(sheet, 30).is_attended)
        self.assertTrue(self._line_on(sheet, 31).is_attended)

    def test_mark_all_absent_forfeits_month_end_rest_day(self):
        """The reported case: supervisor presses 'Mark All Absent' and the
        trailing rest day of the month must not stay present."""
        sheet = self._create_sheet()
        sheet.action_mark_all_absent()

        last_day = monthrange(sheet.year, int(sheet.month))[1]
        self.assertFalse(self._line_on(sheet, last_day).is_attended)
        self.assertEqual(sheet.total_attended, 0)

    def test_off_day_derivation_respects_filter_hook(self):
        """The derivation must route through _filter_derivable_off_days.

        KSW_unpaid_leave pins days covered by an approved leave via
        x_leave_id and its write() refuses any change to them. Without
        the hook, re-deriving such a day raises UserError and aborts the
        whole 'Mark All Absent' action.
        """
        sheet = self._create_sheet()
        protected = self._line_on(sheet, 31)
        line_model = type(self.env['ksw.attendance.sheet.line'])
        self.patch(line_model, '_filter_derivable_off_days',
                   lambda records: records - protected)

        sheet.action_mark_all_absent()  # must not raise

        self.assertTrue(
            protected.is_attended,
            "A day excluded by the hook must be left untouched")
        self.assertFalse(
            self._line_on(sheet, 30).is_attended,
            "The rest of the block must still be derived normally")

    def test_mark_all_absent_raises_if_confirmed(self):
        """Cannot mark absent on a confirmed sheet."""
        sheet = self._create_sheet()
        sheet._do_confirm()
        with self.assertRaises(UserError):
            sheet.action_mark_all_absent()

    def test_mark_all_present_raises_if_confirmed(self):
        """Cannot mark present on a confirmed sheet."""
        sheet = self._create_sheet()
        sheet._do_confirm()
        with self.assertRaises(UserError):
            sheet.action_mark_all_present()

    # ------------------------------------------------------------------
    # Test: Confirm (_do_confirm)
    # ------------------------------------------------------------------

    def test_confirm_creates_attendance_records(self):
        """Confirming a sheet should ensure hr.attendance exists for attended lines."""
        sheet = self._create_sheet()
        sheet._do_confirm()

        self.assertEqual(sheet.state, 'confirmed')
        self.assertTrue(sheet.is_locked)

        attended_lines = sheet.line_ids.filtered('is_attended')
        for line in attended_lines:
            self.assertTrue(
                line.attendance_id,
                f"Attended line {line.date} should have an attendance record.",
            )
            self.assertTrue(line.attendance_id.x_is_auto_generated)

    def test_confirm_attended_has_worked_hours(self):
        """Attended days should have attendance with positive worked_hours."""
        sheet = self._create_sheet()
        sheet._do_confirm()

        attended_lines = sheet.line_ids.filtered('is_attended')
        for line in attended_lines[:5]:  # check first 5
            att = line.attendance_id
            self.assertTrue(att.check_out, "Attended record should have check_out.")

    def test_confirm_absent_has_no_attendance(self):
        """Absent days should not have hr.attendance records."""
        sheet = self._create_sheet()
        # Mark first line as absent
        first_line = sheet.line_ids[0]
        first_line.write({'is_attended': False})
        sheet._do_confirm()

        self.assertFalse(
            first_line.attendance_id,
            "Absent line should not have an attendance record.",
        )

    def test_confirm_skips_already_confirmed(self):
        """Calling _do_confirm on already confirmed sheet should be a no-op."""
        sheet = self._create_sheet()
        sheet._do_confirm()
        # Should not raise
        sheet._do_confirm()
        self.assertEqual(sheet.state, 'confirmed')

    def test_confirm_skips_existing_attendance(self):
        """If an attendance already exists for a day, it should link it
        rather than create a duplicate."""
        sheet = self._create_sheet()
        first_workday = sheet.line_ids.filtered('is_workday')[0]
        d = first_workday.date

        # The line already has an auto-generated attendance from creation.
        # Delete it so we can test the pre-existing record scenario.
        if first_workday.attendance_id:
            first_workday.attendance_id.sudo().unlink()
            first_workday.sudo().write({'attendance_id': False})

        # Pre-create an attendance record for this day
        emp_tz = sheet._get_employee_tz(self.employee)
        import pytz
        local_ci = emp_tz.localize(dt(d.year, d.month, d.day, 8, 0))
        local_co = emp_tz.localize(dt(d.year, d.month, d.day, 16, 30))
        ci_utc = local_ci.astimezone(pytz.utc).replace(tzinfo=None)
        co_utc = local_co.astimezone(pytz.utc).replace(tzinfo=None)

        existing_att = self.env['hr.attendance'].create({
            'employee_id': self.employee.id,
            'check_in': ci_utc,
            'check_out': co_utc,
        })

        sheet._do_confirm()

        # The line should link to the pre-existing attendance
        self.assertEqual(
            first_workday.attendance_id.id, existing_att.id,
            "Should reuse the existing attendance record, not create a new one.",
        )

    # ------------------------------------------------------------------
    # Test: Reset to draft
    # ------------------------------------------------------------------

    def test_reset_to_draft(self):
        """Resetting to draft should re-sync attendance records."""
        sheet = self._create_sheet()
        sheet._do_confirm()

        att_ids = sheet.line_ids.filtered('is_attended').mapped('attendance_id').ids
        self.assertTrue(att_ids, "Should have attendance records after confirm.")

        sheet.action_reset_to_draft()

        self.assertEqual(sheet.state, 'draft')
        self.assertFalse(sheet.is_locked)

        # Auto-generated records from confirm are deleted and re-created
        for line in sheet.line_ids.filtered('is_attended'):
            self.assertTrue(
                line.attendance_id,
                f"Attended line {line.date} should have attendance after reset re-sync.",
            )

    def test_reset_raises_if_not_confirmed(self):
        """Cannot reset a draft sheet."""
        sheet = self._create_sheet()
        with self.assertRaises(UserError):
            sheet.action_reset_to_draft()

    def test_reset_preserves_non_auto_attendance(self):
        """Reset should not delete attendance records that aren't auto-generated."""
        sheet = self._create_sheet()
        first_workday = sheet.line_ids.filtered('is_workday')[0]
        d = first_workday.date

        # Delete the auto-generated attendance to make room for our manual one
        if first_workday.attendance_id:
            first_workday.attendance_id.sudo().unlink()
            first_workday.sudo().write({'attendance_id': False})

        # Pre-create a non-auto-generated attendance
        emp_tz = sheet._get_employee_tz(self.employee)
        import pytz
        local_ci = emp_tz.localize(dt(d.year, d.month, d.day, 8, 0))
        ci_utc = local_ci.astimezone(pytz.utc).replace(tzinfo=None)

        manual_att = self.env['hr.attendance'].create({
            'employee_id': self.employee.id,
            'check_in': ci_utc,
            'check_out': ci_utc + timedelta(hours=8),
            'x_is_auto_generated': False,
        })

        sheet._do_confirm()
        sheet.action_reset_to_draft()

        # The manual attendance should still exist
        self.assertTrue(
            manual_att.exists(),
            "Non-auto-generated attendance should not be deleted on reset.",
        )

    # ------------------------------------------------------------------
    # Test: Attendance sync on toggle
    # ------------------------------------------------------------------

    def test_toggle_attended_syncs_attendance(self):
        """Toggling is_attended should create/delete hr.attendance records."""
        sheet = self._create_sheet()
        line = sheet.line_ids[0]

        # Line should start attended with an attendance record
        self.assertTrue(line.is_attended)
        self.assertTrue(line.attendance_id)
        att_id = line.attendance_id.id

        # Toggle OFF
        line.write({'is_attended': False})
        self.assertFalse(line.attendance_id)
        self.assertFalse(
            self.env['hr.attendance'].search([('id', '=', att_id)]),
            "Attendance record should be deleted when marked absent.",
        )

        # Toggle ON
        line.write({'is_attended': True})
        self.assertTrue(line.attendance_id)
        self.assertTrue(line.attendance_id.x_is_auto_generated)

    # ------------------------------------------------------------------
    # Test: Locked sheet prevents edits
    # ------------------------------------------------------------------

    def test_locked_line_cannot_be_edited(self):
        """Writing is_attended on a locked/confirmed line should raise."""
        sheet = self._create_sheet()
        sheet._do_confirm()

        line = sheet.line_ids.filtered('is_workday')[0]
        with self.assertRaises(UserError):
            line.write({'is_attended': False})

    # ------------------------------------------------------------------
    # Test: Unique constraint
    # ------------------------------------------------------------------

    def test_unique_employee_month_year(self):
        """Cannot create two sheets for the same employee/month/year."""
        self._create_sheet()
        with self.assertRaises(Exception):
            self._create_sheet()

    # ------------------------------------------------------------------
    # Test: Display name
    # ------------------------------------------------------------------

    def test_display_name(self):
        """Display name should be 'Employee Name - Month Year'."""
        sheet = self._create_sheet()
        self.assertIn(self.employee.name, sheet.display_name)
        self.assertIn('January', sheet.display_name)
        self.assertIn(str(self.test_year), sheet.display_name)

    # ------------------------------------------------------------------
    # Test: Employee toggle auto-creates sheet
    # ------------------------------------------------------------------

    def test_toggle_attendance_sheet_creates_sheet(self):
        """Toggling x_is_attendance_sheet ON should create current month's sheet."""
        today = fields.Date.context_today(self.env['hr.employee'])
        month = str(today.month)
        year = today.year

        emp = self.env['hr.employee'].create({
            'name': 'Toggle Test Employee',
            'resource_calendar_id': self.calendar.id,
            'parent_id': self.manager_employee.id,
            'x_is_attendance_sheet': False,
        })

        # Verify no sheet exists
        sheets_before = self.env['ksw.attendance.sheet'].search([
            ('employee_id', '=', emp.id),
            ('month', '=', month),
            ('year', '=', year),
        ])
        self.assertFalse(sheets_before)

        # Toggle ON
        emp.write({'x_is_attendance_sheet': True})

        sheets_after = self.env['ksw.attendance.sheet'].search([
            ('employee_id', '=', emp.id),
            ('month', '=', month),
            ('year', '=', year),
        ])
        self.assertEqual(len(sheets_after), 1)
        self.assertTrue(len(sheets_after.line_ids) > 0)

    def test_toggle_idempotent(self):
        """Toggling x_is_attendance_sheet ON twice should not create duplicate."""
        today = fields.Date.context_today(self.env['hr.employee'])
        month = str(today.month)
        year = today.year

        emp = self.env['hr.employee'].create({
            'name': 'Toggle Idempotent Employee',
            'resource_calendar_id': self.calendar.id,
            'parent_id': self.manager_employee.id,
            'x_is_attendance_sheet': False,
        })

        emp.write({'x_is_attendance_sheet': True})
        emp.write({'x_is_attendance_sheet': True})  # second toggle

        sheets = self.env['ksw.attendance.sheet'].search([
            ('employee_id', '=', emp.id),
            ('month', '=', month),
            ('year', '=', year),
        ])
        self.assertEqual(len(sheets), 1, "Should not create duplicate sheets.")

    # ------------------------------------------------------------------
    # Test: Cron
    # ------------------------------------------------------------------

    def test_cron_generates_sheets_for_current_month(self):
        """Cron should create sheets for employees with x_is_attendance_sheet=True."""
        self.employee.write({'x_is_attendance_sheet': True})

        # Delete any existing sheet the toggle might have created
        today = fields.Date.context_today(self.env['hr.employee'])
        existing = self.env['ksw.attendance.sheet'].search([
            ('employee_id', '=', self.employee.id),
            ('month', '=', str(today.month)),
            ('year', '=', today.year),
        ])
        existing.unlink()

        self.env['ksw.attendance.sheet']._cron_generate_sheets()

        sheets = self.env['ksw.attendance.sheet'].search([
            ('employee_id', '=', self.employee.id),
            ('month', '=', str(today.month)),
            ('year', '=', today.year),
        ])
        self.assertEqual(len(sheets), 1)

    def test_cron_autoconfirms_past_month_drafts(self):
        """Cron should auto-confirm draft sheets from previous months."""
        # Create a sheet for a past month
        past_sheet = self.env['ksw.attendance.sheet'].create({
            'employee_id': self.employee.id,
            'month': '12',  # December
            'year': 2025,
        })
        self.assertEqual(past_sheet.state, 'draft')

        self.env['ksw.attendance.sheet']._cron_generate_sheets()

        past_sheet.invalidate_recordset()
        self.assertEqual(
            past_sheet.state, 'confirmed',
            "Past-month draft sheets should be auto-confirmed by the cron.",
        )
        self.assertTrue(past_sheet.is_locked)

    def test_cron_does_not_duplicate_sheets(self):
        """Cron should not create a sheet if one already exists."""
        self.employee.write({'x_is_attendance_sheet': True})
        today = fields.Date.context_today(self.env['hr.employee'])

        # Run cron twice
        self.env['ksw.attendance.sheet']._cron_generate_sheets()
        self.env['ksw.attendance.sheet']._cron_generate_sheets()

        sheets = self.env['ksw.attendance.sheet'].search([
            ('employee_id', '=', self.employee.id),
            ('month', '=', str(today.month)),
            ('year', '=', today.year),
        ])
        self.assertEqual(len(sheets), 1)

    # ------------------------------------------------------------------
    # Test: Schedule helpers
    # ------------------------------------------------------------------

    def test_is_workday_returns_true_for_scheduled(self):
        """_is_workday should return True for a scheduled work day."""
        sheet = self._create_sheet()
        # January 5, 2026 is a Monday (weekday=0) — should be a work day
        d = fields.Date.to_date('2026-01-05')
        self.assertTrue(sheet._is_workday(self.employee, d))

    def test_is_workday_returns_false_for_weekend(self):
        """_is_workday should return False for non-scheduled days (Fri/Sat)."""
        sheet = self._create_sheet()
        # January 2, 2026 is a Friday (weekday=4) — not a work day
        d = fields.Date.to_date('2026-01-02')
        self.assertFalse(sheet._is_workday(self.employee, d))

    def test_get_work_schedule(self):
        """_get_work_schedule should return schedule details for a work day."""
        sheet = self._create_sheet()
        d = fields.Date.to_date('2026-01-05')  # Monday
        schedule = sheet._get_work_schedule(self.employee, d)

        self.assertIsNotNone(schedule)
        self.assertAlmostEqual(schedule['hour_from'], 8.0)
        self.assertAlmostEqual(schedule['hour_to'], 16.5)
        self.assertAlmostEqual(schedule['break_hours'], 1.0)

    def test_get_work_schedule_returns_none_for_weekend(self):
        """_get_work_schedule should return None for non-work days."""
        sheet = self._create_sheet()
        d = fields.Date.to_date('2026-01-02')  # Friday
        schedule = sheet._get_work_schedule(self.employee, d)
        self.assertIsNone(schedule)

    # ------------------------------------------------------------------
    # Test: Generate lines (regeneration)
    # ------------------------------------------------------------------

    def test_generate_lines_replaces_existing(self):
        """action_generate_lines should delete existing lines and regenerate."""
        sheet = self._create_sheet()
        original_count = len(sheet.line_ids)
        original_ids = set(sheet.line_ids.ids)

        sheet.action_generate_lines()

        self.assertEqual(len(sheet.line_ids), original_count)
        # Lines should be new records (different IDs)
        new_ids = set(sheet.line_ids.ids)
        self.assertFalse(
            original_ids & new_ids,
            "Regenerated lines should be new records.",
        )

    def test_generate_lines_raises_if_confirmed(self):
        """Cannot regenerate lines on a confirmed sheet."""
        sheet = self._create_sheet()
        sheet._do_confirm()

        with self.assertRaises(UserError):
            sheet.action_generate_lines()

    # ------------------------------------------------------------------
    # Test: Employee timezone
    # ------------------------------------------------------------------

    def test_get_employee_tz(self):
        """_get_employee_tz should return the calendar's timezone."""
        sheet = self._create_sheet()
        tz = sheet._get_employee_tz(self.employee)
        self.assertEqual(str(tz), 'Asia/Riyadh')

    # ------------------------------------------------------------------
    # Test: Cron resilience (savepoints)
    # ------------------------------------------------------------------

    def test_cron_savepoint_resilience(self):
        """A sheet that errors during confirmation must not block the rest."""
        from unittest.mock import patch

        emp2 = self.env['hr.employee'].create({
            'name': 'Cron Resilience Emp 2',
            'resource_calendar_id': self.calendar.id,
        })
        sheet_bad = self._create_sheet(month='12', year=2025)
        sheet_good = self._create_sheet(employee=emp2, month='12', year=2025)
        bad_id = sheet_bad.id

        Model = type(sheet_bad)
        orig = Model._do_confirm

        def patched(inner_self):
            if inner_self.id == bad_id:
                raise ValueError('forced failure')
            return orig(inner_self)

        with patch.object(Model, '_do_confirm', patched):
            self.env['ksw.attendance.sheet']._cron_generate_sheets()

        sheet_bad.invalidate_recordset()
        sheet_good.invalidate_recordset()
        self.assertEqual(sheet_bad.state, 'draft',
                         "Failed sheet must remain draft (savepoint rolled it back)")
        self.assertEqual(sheet_good.state, 'confirmed',
                         "Good sheet must be confirmed despite the other failing")

    def test_cron_confirm_skips_insync_lines(self):
        """_do_confirm must not create duplicate attendance records for in-sync lines."""
        sheet = self._create_sheet(month='12', year=2025)
        # action_generate_lines (called on create) already synced all attended lines
        att_count_before = self.env['hr.attendance'].search_count([
            ('employee_id', '=', self.employee.id),
        ])
        self.assertGreater(att_count_before, 0, "Lines should have auto-generated attendance records")

        self.env['ksw.attendance.sheet']._cron_generate_sheets()

        att_count_after = self.env['hr.attendance'].search_count([
            ('employee_id', '=', self.employee.id),
        ])
        sheet.invalidate_recordset()
        self.assertEqual(sheet.state, 'confirmed')
        self.assertEqual(att_count_before, att_count_after,
                         "_do_confirm must not re-create attendance records for already-synced lines")
