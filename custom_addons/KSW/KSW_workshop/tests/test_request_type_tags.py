"""Tags derived from the description, and the register that produces them.

The requester is never asked to classify his own fault — he writes what is
wrong and the keywords do the rest. What matters here is that the rules are
configuration (a manager adding a word changes what new requests get, with no
deploy), that a job can carry several types at once, and above all that a
human correction is never undone by the next edit to the description.
"""
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestRequestTypeTags(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _mkuser(name, login, group_xmlids=('base.group_user',)):
            return cls.env['res.users'].create({
                'name': name, 'login': login, 'email': f'{login}@wstags.test',
                'group_ids': [(6, 0, [cls.env.ref(x).id for x in group_xmlids])],
            })

        cls.user_employee = _mkuser('WST Employee', 'wstags_employee')
        cls.employee = cls.env['hr.employee'].create({
            'name': 'WST Employee', 'user_id': cls.user_employee.id,
        })
        cls.user_manager = _mkuser(
            'WST Manager', 'wstags_manager',
            group_xmlids=('base.group_user', 'KSW_workshop.group_workshop_manager'),
        )
        cls.env['hr.employee'].create({'name': 'WST Manager', 'user_id': cls.user_manager.id})

        cls.vehicle = cls.env['ksw.fleet.vehicle'].create({
            'name': 'WST-401', 'vehicle_type': 'isuzu',
        })
        cls.oil = cls.env.ref('KSW_workshop.request_type_oil_filters')
        cls.brakes = cls.env.ref('KSW_workshop.request_type_brakes')
        cls.tyres = cls.env.ref('KSW_workshop.request_type_tyres')

    def _make(self, description, user=None, **kwargs):
        vals = {
            'vehicle_id': self.vehicle.id,
            'vehicle_type': 'isuzu',
            'driver_id': self.employee.id,
            'description': description,
        }
        vals.update(kwargs)
        model = self.env['ksw.workshop.request']
        return model.with_user(user or self.user_employee).create(vals)

    # ------------------------------------------------------------------
    # Deriving
    # ------------------------------------------------------------------
    def test_description_alone_produces_a_tag(self):
        request = self._make('يحتاج تغيير زيت')
        self.assertEqual(request.request_type_ids, self.oil)
        self.assertTrue(request.x_request_type_derived)

    def test_a_job_can_carry_several_tags(self):
        """The whole reason this is a Many2many: 872 of the imported
        descriptions match more than one rule, and the Selection kept only the
        first one."""
        request = self._make('تغيير زيت وتغيير قماشات فرامل')
        self.assertEqual(request.request_type_ids, self.oil | self.brakes)

    def test_misspellings_count(self):
        """'زبت' for 'زيت' and 'chance oil' for 'change oil' are how the
        supervisors actually type them — hundreds of real requests."""
        self.assertEqual(self._make('زبت').request_type_ids, self.oil)
        self.assertEqual(self._make('CHANCE OIL please').request_type_ids, self.oil)

    def test_a_keyword_matches_inside_a_longer_word(self):
        self.assertEqual(self._make('تغيير الفلتر').request_type_ids, self.oil)

    def test_no_match_leaves_it_unclassified(self):
        """An honest gap, not a catch-all bucket — the same rule the history
        backfill followed when it left its 26% tail NULL."""
        request = self._make('صوت غريب')
        self.assertFalse(request.request_type_ids)

    def test_unclassified_filter_finds_it(self):
        request = self._make('صوت غريب')
        found = self.env['ksw.workshop.request'].sudo().search([
            ('id', '=', request.id), ('request_type_ids', '=', False),
        ])
        self.assertEqual(found, request)

    # ------------------------------------------------------------------
    # The rules are configuration
    # ------------------------------------------------------------------
    def test_a_new_keyword_changes_what_new_requests_get(self):
        """The point of moving the rules out of Python: no deploy, no upgrade."""
        before = self._make('the flambulator is broken')
        self.assertFalse(before.request_type_ids)
        self.brakes.with_user(self.user_manager).keywords += ', flambulator'
        after = self._make('the flambulator is broken')
        self.assertEqual(after.request_type_ids, self.brakes)

    def test_a_requester_cannot_edit_the_rules(self):
        with self.assertRaises(Exception):
            self.brakes.with_user(self.user_employee).write({'keywords': 'anything'})

    def test_keyword_terms_are_words_not_regexes(self):
        """A manager typing 'brake (front)' is entering words. An unescaped
        '(' would make the whole pattern uncompilable and take every other
        tag down with it."""
        self.tyres.keywords = 'tyre (front), كفر'
        self.assertEqual(self._make('replace tyre (front)').request_type_ids, self.tyres)

    # ------------------------------------------------------------------
    # Corrections outrank the rules
    # ------------------------------------------------------------------
    def test_editing_the_description_retags_while_still_new(self):
        request = self._make('تغيير زيت')
        self.assertEqual(request.request_type_ids, self.oil)
        request.with_user(self.user_employee).write({'description': 'بنشر في الكفر'})
        self.assertEqual(request.request_type_ids, self.tyres)

    def test_a_manual_correction_is_never_re_derived_away(self):
        request = self._make('تغيير زيت')
        request.with_user(self.user_manager).write({
            'request_type_ids': [(6, 0, self.brakes.ids)]})
        self.assertFalse(request.x_request_type_derived)
        # The description changes again — the correction must survive it.
        request.with_user(self.user_manager).write({'description': 'تغيير زيت وفلتر'})
        self.assertEqual(request.request_type_ids, self.brakes)

    def test_requester_cannot_set_the_tags(self):
        request = self._make('تغيير زيت')
        with self.assertRaises(UserError):
            request.with_user(self.user_employee).write({
                'request_type_ids': [(6, 0, self.brakes.ids)]})

    def test_explicit_tags_on_create_are_treated_as_chosen(self):
        request = self._make(
            'تغيير زيت', user=self.user_manager,
            request_type_ids=[(6, 0, self.tyres.ids)])
        self.assertEqual(request.request_type_ids, self.tyres)
        self.assertFalse(request.x_request_type_derived)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def test_tags_are_groupable(self):
        """The By Repair Type report groups on this field — a Many2many has to
        survive _read_group, which is not true of every field type."""
        self._make('تغيير زيت وقماشات فرامل')
        grouped = self.env['ksw.workshop.request'].sudo()._read_group(
            [('request_type_ids', 'in', (self.oil | self.brakes).ids)],
            ['request_type_ids'], ['__count'],
        )
        counted = {rtype.id: count for rtype, count in grouped}
        self.assertIn(self.oil.id, counted)
        self.assertIn(self.brakes.id, counted)

    def test_seeded_types_carry_keywords_except_the_manual_one(self):
        """Coupling / Fifth Wheel is the single exception, on purpose: VMRS 059
        is a real system for a trailer fleet, but no wording for it appears in
        17,080 requests, so it is there to be picked by hand rather than to
        guess at Arabic nobody uses."""
        types = self.env['ksw.workshop.request.type'].search([])
        self.assertGreaterEqual(len(types), 26)
        without = types.filtered(lambda t: not t.keyword_pattern())
        self.assertEqual(without, self.env.ref('KSW_workshop.request_type_coupling'))
