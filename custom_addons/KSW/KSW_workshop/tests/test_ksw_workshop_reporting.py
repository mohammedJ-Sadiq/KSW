"""Reporting surface: derived request types, stored measures, vehicle counts."""
import re

from odoo.tests.common import TransactionCase

from odoo.addons.KSW_workshop.models.ksw_workshop_request import REQUEST_TYPE_KEYWORDS


class TestWorkshopReporting(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.vehicle = cls.env['ksw.fleet.vehicle'].create({
            'name': 'RPT-001', 'vehicle_type': 'isuzu',
        })
        cls.other_vehicle = cls.env['ksw.fleet.vehicle'].create({
            'name': 'RPT-002', 'vehicle_type': 'isuzu',
        })
        # employee_id is required and defaults to env.user.employee_id, which
        # is empty for the user running the tests — set it explicitly.
        cls.requester = cls.env['hr.employee'].create({'name': 'RPT Requester'})

    def _make(self, description='Something broke', **kwargs):
        vals = {
            'vehicle_id': self.vehicle.id,
            'description': description,
            'employee_id': self.requester.id,
        }
        vals.update(kwargs)
        return self.env['ksw.workshop.request'].create(vals)

    # ------------------------------------------------------------------
    # Keyword classification (the migration's rules, exercised in Python)
    # ------------------------------------------------------------------
    def _classify(self, description):
        """Mirror of the migration: first matching pattern wins."""
        for request_type, pattern in REQUEST_TYPE_KEYWORDS:
            if re.search(pattern, description, re.IGNORECASE):
                return request_type
        return None

    def test_classifier_matches_arabic_oil(self):
        self.assertEqual(self._classify('غيار زيت وفلتر'), 'oil_filters')

    def test_classifier_matches_arabic_oil_misspelling(self):
        # 'زبت' for 'زيت' is how the supervisors actually typed it — this
        # misspelling alone is dozens of rows in the history.
        self.assertEqual(self._classify('غيار زبت'), 'oil_filters')

    def test_classifier_matches_english_oil(self):
        self.assertEqual(self._classify('Change oil'), 'oil_filters')
        self.assertEqual(self._classify('Chance oil'), 'oil_filters')

    def test_classifier_matches_other_categories(self):
        self.assertEqual(self._classify('تبديل اطار'), 'tyres')
        self.assertEqual(self._classify('اصلاح الفرامل'), 'brakes')
        self.assertEqual(self._classify('مشكلة كهرباء'), 'electrical')
        self.assertEqual(self._classify('لحام الشاسيه'), 'bodywork')
        self.assertEqual(self._classify('اصلاح موتور المياه'), 'water_pump')
        self.assertEqual(self._classify('هوز كبير'), 'hoses')
        self.assertEqual(self._classify('الفحص الدوري'), 'inspection')

    def test_classifier_priority_oil_wins_over_later_category(self):
        # ~400 descriptions mention more than one kind of work; ordering in
        # REQUEST_TYPE_KEYWORDS is what decides, so it must be deterministic.
        self.assertEqual(self._classify('غيار زيت وتبديل اطار'), 'oil_filters')

    def test_classifier_leaves_unknown_unclassified(self):
        # Deliberately None, not a catch-all 'other' — a report must not imply
        # a classification nobody made.
        self.assertIsNone(self._classify('تجهيز السياره بالكامل'))

    def test_every_keyword_pattern_is_a_valid_regex(self):
        # The same strings are fed to PostgreSQL `~*` by the migration; a
        # broken pattern there fails mid-upgrade.
        for request_type, pattern in REQUEST_TYPE_KEYWORDS:
            with self.subTest(request_type=request_type):
                re.compile(pattern)

    def test_every_keyword_type_is_a_real_selection_value(self):
        valid = dict(self.env['ksw.workshop.request']._fields['request_type'].selection)
        for request_type, _pattern in REQUEST_TYPE_KEYWORDS:
            self.assertIn(request_type, valid)

    # ------------------------------------------------------------------
    # Stored measures
    # ------------------------------------------------------------------
    def test_total_cost_is_stored_and_summed(self):
        request = self._make(parts_cost=250.0, labor_cost=100.0)
        self.assertEqual(request.total_cost, 350.0)
        # Stored => searchable. An unstored compute would raise here, which is
        # exactly what made duration_days unusable as a pivot measure.
        found = self.env['ksw.workshop.request'].search([
            ('id', '=', request.id), ('total_cost', '=', 350.0),
        ])
        self.assertEqual(found, request)

    def test_total_cost_recomputes_on_change(self):
        request = self._make(parts_cost=100.0, labor_cost=0.0)
        request.labor_cost = 60.0
        self.assertEqual(request.total_cost, 160.0)

    def test_duration_days_is_stored(self):
        # The whole point of the store=True change: an unstored compute cannot
        # be a pivot measure, grouped or sorted.
        field = self.env['ksw.workshop.request']._fields['duration_days']
        self.assertTrue(field.store, 'duration_days must be stored to be a measure')

    def test_duration_days_counts_whole_days(self):
        # Explicit dates rather than action_complete(): the test harness
        # freezes create_date while completion_date takes the real clock, so a
        # same-instant completion yields a sub-second negative timedelta whose
        # .days is -1, not 0. Real history spans days, so pin the dates here
        # and let the arithmetic be the thing under test.
        request = self._make()
        request.action_start()
        request.action_complete()
        request.sudo().write({'completion_date': '2026-03-10 08:00:00'})
        self.env.cr.execute(
            "UPDATE ksw_workshop_request SET create_date = %s WHERE id = %s",
            ('2026-03-07 08:00:00', request.id),
        )
        request.invalidate_recordset()
        request.modified(['create_date', 'completion_date'])
        self.assertEqual(request.duration_days, 3)

    def test_duration_days_is_searchable_when_stored(self):
        request = self._make()
        request.sudo().write({'completion_date': '2026-03-10 08:00:00'})
        self.env.cr.execute(
            "UPDATE ksw_workshop_request SET create_date = %s WHERE id = %s",
            ('2026-03-07 08:00:00', request.id),
        )
        request.invalidate_recordset()
        request.modified(['create_date', 'completion_date'])
        self.env.flush_all()
        found = self.env['ksw.workshop.request'].search([
            ('id', '=', request.id), ('duration_days', '=', 3),
        ])
        self.assertEqual(found, request)

    # ------------------------------------------------------------------
    # technician_label — the single grouping axis across both eras
    # ------------------------------------------------------------------
    def test_technician_label_uses_linked_employee(self):
        tech = self.env['hr.employee'].create({'name': 'Real Technician'})
        request = self._make(technician_id=tech.id)
        self.assertEqual(request.technician_label, 'Real Technician')

    def test_technician_label_falls_back_to_legacy_name(self):
        request = self._make()
        request.x_legacy_technician_name = 'سلطان'
        self.assertEqual(request.technician_label, 'سلطان')

    def test_technician_label_prefers_employee_over_legacy_name(self):
        tech = self.env['hr.employee'].create({'name': 'Real Technician'})
        request = self._make()
        request.x_legacy_technician_name = 'سلطان'
        request.technician_id = tech.id
        self.assertEqual(request.technician_label, 'Real Technician')

    def test_technician_label_is_groupable(self):
        request = self._make()
        request.x_legacy_technician_name = 'بلال'
        groups = self.env['ksw.workshop.request']._read_group(
            [('id', '=', request.id)], ['technician_label'], ['__count'],
        )
        self.assertEqual(groups[0][0], 'بلال')

    # ------------------------------------------------------------------
    # Vehicle visit count
    # ------------------------------------------------------------------
    def test_vehicle_request_count(self):
        self._make()
        self._make()
        self._make(vehicle_id=self.other_vehicle.id)
        self.assertEqual(self.vehicle.workshop_request_count, 2)
        self.assertEqual(self.other_vehicle.workshop_request_count, 1)

    def test_vehicle_count_is_zero_without_requests(self):
        fresh = self.env['ksw.fleet.vehicle'].create({
            'name': 'RPT-003', 'vehicle_type': 'tank',
        })
        self.assertEqual(fresh.workshop_request_count, 0)

    def test_vehicle_action_filters_to_that_vehicle(self):
        request = self._make()
        action = self.vehicle.action_view_workshop_requests()
        self.assertEqual(action['res_model'], 'ksw.workshop.request')
        found = self.env['ksw.workshop.request'].search(action['domain'])
        self.assertIn(request, found)
