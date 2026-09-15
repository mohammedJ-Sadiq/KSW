# -*- coding: utf-8 -*-
"""Keeping the keyword rules alive: re-scanning history, and finding new wording.

The rules are only as good as the last time someone improved them. These
guard the two mechanisms that let that happen — applying an improvement
backwards over the history, and surfacing the wording nobody has covered yet —
and above all the line neither of them may cross: a request somebody has
classified by hand is never re-classified by a rule.
"""
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestKeywordMaintenance(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user_manager = cls.env['res.users'].create({
            'name': 'WSK Manager', 'login': 'wskw_manager', 'email': 'wskw@kw.test',
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('KSW_workshop.group_workshop_manager').id,
            ])],
        })
        cls.env['hr.employee'].create({'name': 'WSK Manager', 'user_id': cls.user_manager.id})
        cls.employee = cls.env['hr.employee'].create({'name': 'WSK Requester'})
        cls.vehicle = cls.env['ksw.fleet.vehicle'].create({
            'name': 'WSK-501', 'vehicle_type': 'isuzu'})
        cls.oil = cls.env.ref('KSW_workshop.request_type_oil_filters')
        cls.brakes = cls.env.ref('KSW_workshop.request_type_brakes')
        # A type of our own, so a re-scan in a test cannot disturb the seeded
        # rules the rest of the suite reads.
        cls.scratch = cls.env['ksw.workshop.request.type'].create({
            'name': 'WSK Scratch', 'keywords': 'zzqq'})

    def _make(self, description, **kwargs):
        vals = {
            'employee_id': self.employee.id, 'vehicle_id': self.vehicle.id,
            'vehicle_type': 'isuzu', 'driver_id': self.employee.id,
            'description': description,
        }
        vals.update(kwargs)
        return self.env['ksw.workshop.request'].create(vals)

    # ------------------------------------------------------------------
    # Re-scan
    # ------------------------------------------------------------------
    def test_rescan_applies_a_new_keyword_to_history(self):
        """The reason the button exists: an improvement must reach the
        requests that are already waiting for it."""
        request = self._make('the flambulator is broken')
        self.assertFalse(request.request_type_ids)
        self.scratch.keywords = 'zzqq, flambulator'
        added, _removed = self.scratch._rescan_requests()
        self.assertGreaterEqual(added, 1)
        self.assertIn(self.scratch, request.request_type_ids)

    def test_rescan_removes_a_tag_that_no_longer_matches(self):
        """A re-scan is 'what would the rules say today', not 'add more tags' —
        or fixing a mistyped keyword would leave its wrong tags behind."""
        self.scratch.keywords = 'flambulator'
        request = self._make('the flambulator is broken')
        self.assertIn(self.scratch, request.request_type_ids)
        self.scratch.keywords = 'something else entirely'
        _added, removed = self.scratch._rescan_requests()
        self.assertGreaterEqual(removed, 1)
        self.assertNotIn(self.scratch, request.request_type_ids)

    def test_rescan_never_touches_a_hand_set_request(self):
        request = self._make('the flambulator is broken')
        request.with_user(self.user_manager).write({
            'request_type_ids': [(6, 0, self.brakes.ids)]})
        self.assertFalse(request.x_request_type_derived)
        self.scratch.keywords = 'flambulator'
        self.scratch._rescan_requests()
        self.assertEqual(request.request_type_ids, self.brakes)

    def test_rescan_treats_a_null_derived_flag_as_derived(self):
        """A Boolean added later leaves NULL on every existing row, and NULL is
        not FALSE — but `WHERE bool_col` drops it and Python reads it as False.
        The 4,454 untagged legacy requests were exactly those rows: invisible
        to both mechanisms meant to improve them."""
        request = self._make('the flambulator is broken')
        self.env.cr.execute(
            'UPDATE ksw_workshop_request SET x_request_type_derived = NULL WHERE id = %s',
            (request.id,),
        )
        request.invalidate_recordset(['x_request_type_derived'])
        self.scratch.keywords = 'flambulator'
        self.scratch._rescan_requests()
        self.assertIn(self.scratch, request.request_type_ids)

    def test_rescan_button_reports_what_it_did(self):
        self.scratch.keywords = 'flambulator'
        self._make('the flambulator is broken')
        action = self.scratch.with_user(self.user_manager).action_rescan_requests()
        self.assertEqual(action['tag'], 'display_notification')

    # ------------------------------------------------------------------
    # Suggestions
    # ------------------------------------------------------------------
    def test_untagged_words_are_counted(self):
        self._make('the flambulator is broken')
        self._make('another flambulator fault')
        counts, samples = self.env['ksw.workshop.keyword.suggestion']._collect()
        self.assertEqual(counts.get('flambulator'), 2)
        self.assertTrue(samples.get('flambulator'))

    def test_a_tagged_request_contributes_no_suggestions(self):
        """Suggestions come from the unclassified pile only — a word already
        covered by a rule is not a gap."""
        self._make('تغيير زيت flambulator')      # tagged Oil & Filters
        counts, _samples = self.env['ksw.workshop.keyword.suggestion']._collect()
        self.assertIsNone(counts.get('flambulator'))

    def test_noise_words_are_not_suggested(self):
        self._make('تريلا 55 مشكله في flambulator')
        counts, _samples = self.env['ksw.workshop.keyword.suggestion']._collect()
        self.assertIsNone(counts.get('تريلا'))
        self.assertIsNone(counts.get('مشكله'))
        self.assertEqual(counts.get('flambulator'), 1)

    def test_ignoring_a_word_drops_it_from_later_counts(self):
        self._make('the flambulator is broken')
        Suggestion = self.env['ksw.workshop.keyword.suggestion']
        suggestion = Suggestion.create({'name': 'flambulator', 'request_count': 1})
        suggestion.with_user(self.user_manager).action_ignore()
        counts, _samples = Suggestion._collect()
        self.assertIsNone(counts.get('flambulator'))

    def test_filing_a_word_adds_the_keyword_and_tags_history(self):
        """One action closes the loop: the word becomes a rule, and the
        requests that were waiting for it are tagged."""
        request = self._make('the flambulator is broken')
        self.assertFalse(request.request_type_ids)
        suggestion = self.env['ksw.workshop.keyword.suggestion'].create({
            'name': 'flambulator', 'request_count': 1, 'type_id': self.scratch.id})
        suggestion.with_user(self.user_manager).action_add_to_type()
        self.assertIn('flambulator', self.scratch.keywords)
        self.assertIn(self.scratch, request.request_type_ids)

    def test_filing_without_a_type_says_so(self):
        suggestion = self.env['ksw.workshop.keyword.suggestion'].create({
            'name': 'flambulator', 'request_count': 1})
        with self.assertRaises(UserError):
            suggestion.with_user(self.user_manager).action_add_to_type()

    # ------------------------------------------------------------------
    # The taxonomy itself
    # ------------------------------------------------------------------
    def test_mechanical_catch_all_was_decomposed(self):
        """It used to carry clutch, gearbox, suspension, radiator, A/C and axle
        at once — six VMRS systems in one bucket."""
        self.assertEqual(
            self._make('تغيير كلتش').request_type_ids,
            self.env.ref('KSW_workshop.request_type_clutch'))
        self.assertEqual(
            self._make('مشكله في القير').request_type_ids,
            self.env.ref('KSW_workshop.request_type_transmission'))

    def test_air_on_a_trailer_is_suspension_not_air_intake(self):
        """The correction the corpus forced: هواء on a trailer is the air
        suspension, not the air intake — 269 requests would have been mistagged."""
        self.assertEqual(
            self._make('تريلا 77 مشكله بالهواء').request_type_ids,
            self.env.ref('KSW_workshop.request_type_suspension_air'))

    def test_leak_is_a_symptom_and_tags_nothing_on_its_own(self):
        """تهريب crosses every system — تهريب ديزل, تهريب ميه, تهريب هواء. VMRS,
        SAP PM, Maximo and D365 all keep that axis separate from the component."""
        self.assertFalse(self._make('يوجد تهريب').request_type_ids)

    def test_every_seeded_type_carries_a_vmrs_aligned_name(self):
        types = self.env['ksw.workshop.request.type'].search([])
        self.assertGreaterEqual(len(types), 26)
