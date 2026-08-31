"""Tests for the pay component catalog, the rate resolver and batches.

The resolver is the heart of the redesign: every pay type in KSW now goes
through it, so an error here is an error in everyone's pay.
"""
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase


class PayEntryCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.period = '2028-07-01'
        cls.dept = env['hr.department'].create({'name': 'Pay Dept'})
        cls.other_dept = env['hr.department'].create({'name': 'Pay Dept 2'})
        cls.site = env['ksw.site'].create({'name': 'Pay Site', 'code': 'PS'})

        cls.emp = cls._employee('Pay Emp', cls.dept, 7200.0)
        cls.emp2 = cls._employee('Pay Emp 2', cls.dept, 4500.0)

        cls.c_overtime = env.ref('KSW_commissions.pay_component_overtime')
        cls.c_meals = env.ref('KSW_commissions.pay_component_meals')
        cls.o_breakfast = env.ref('KSW_commissions.pay_option_meal_breakfast')
        cls.o_lunch = env.ref('KSW_commissions.pay_option_meal_lunch')
        cls.c_trips = env.ref('KSW_commissions.pay_component_driver_trips')
        cls.c_mobile = env.ref('KSW_commissions.pay_component_mobile')

    @classmethod
    def _employee(cls, name, dept, wage):
        emp = cls.env['hr.employee'].sudo().create({
            'name': name, 'department_id': dept.id,
        })
        if emp.current_version_id:
            emp.current_version_id.sudo().write({'wage': wage})
        return emp

    def _batch(self, component=None, dept=None, site=None, period=None):
        component = component or self.c_overtime
        vals = {
            'component_id': component.id,
            'period': period or self.period,
        }
        if component.scope == 'department':
            vals['department_id'] = (dept or self.dept).id
        elif component.scope == 'site':
            vals['site_id'] = (site or self.site).id
        return self.env['ksw.pay.batch'].sudo().create(vals)

    def _entry(self, batch, employee=None, **kwargs):
        vals = {
            'batch_id': batch.id,
            'employee_id': (employee or self.emp).id,
        }
        if batch.component_id.needs_date:
            vals.setdefault('date', self.period)
        if batch.component_id.needs_reason:
            vals.setdefault('reason', 'test')
        if batch.component_id.has_options:
            vals.setdefault('option_id', batch.component_id.option_ids[0].id)
        vals.update(kwargs)
        return self.env['ksw.pay.entry'].sudo().create(vals)


class TestPayResolver(PayEntryCommon):
    """quantity x rate -> amount, for all four calculation methods."""

    def test_01_wage_rate_formula(self):
        """7200 / 240 x 1.5 x 4 = 180."""
        entry = self._entry(self._batch(), quantity=4.0)
        self.assertAlmostEqual(entry.rate, 45.0, places=4)
        self.assertAlmostEqual(entry.amount, 180.0, places=2)

    def test_02_wage_rate_never_rounds_the_rate_first(self):
        """4500 / 240 x 1.5 = 28.125/h. Four hours is 112.50, not 112.52.

        The regression this whole design decision exists for: computing
        `rate x quantity` with a Monetary rate rounds 28.125 to 28.13.
        """
        entry = self._entry(self._batch(), employee=self.emp2, quantity=4.0)
        self.assertAlmostEqual(entry.amount, 112.50, places=2)

    def test_03_zero_wage_does_not_divide_by_zero(self):
        emp = self._employee('Pay No Wage', self.dept, 0.0)
        entry = self._entry(self._batch(), employee=emp, quantity=4.0)
        self.assertAlmostEqual(entry.amount, 0.0)

    def test_04_qty_rate(self):
        """3 lunches at 20.00 = 60.00."""
        batch = self._batch(component=self.c_meals)
        entry = self._entry(batch, quantity=3.0, option_id=self.o_lunch.id)
        self.assertAlmostEqual(entry.amount, 60.0, places=2)

    def test_05_fixed_keeps_what_was_typed(self):
        batch = self._batch(component=self.c_mobile)
        entry = self._entry(batch, quantity=1.0, amount=100.0)
        self.assertAlmostEqual(entry.amount, 100.0, places=2)

    def test_06_tiered_waterfall(self):
        """Above the free allowance: 40@10 + 40@15 + 40@20 + rest@25."""
        batch = self._batch(component=self.c_trips)
        # 50 free, then 130 earning: 40x10 + 40x15 + 40x20 + 10x25 = 2050
        entry = self._entry(batch, quantity=180.0, threshold_qty=50.0)
        self.assertAlmostEqual(entry.amount, 2050.0, places=2)

    def test_07_tiered_below_threshold_pays_nothing(self):
        batch = self._batch(component=self.c_trips)
        entry = self._entry(batch, quantity=40.0, threshold_qty=50.0)
        self.assertAlmostEqual(entry.amount, 0.0)

    def test_08_tiered_partial_first_band(self):
        batch = self._batch(component=self.c_trips)
        # 50 free, 25 earning, all in the 10.00 band = 250
        entry = self._entry(batch, quantity=75.0, threshold_qty=50.0)
        self.assertAlmostEqual(entry.amount, 250.0, places=2)

    def test_09_override_survives_a_quantity_edit(self):
        entry = self._entry(self._batch(), quantity=4.0)
        entry.write({'amount_override': 500.0})
        self.assertTrue(entry.is_overridden)
        self.assertAlmostEqual(entry.amount, 500.0)
        entry.write({'quantity': 8.0})
        self.assertAlmostEqual(entry.amount, 500.0,
                               msg='the override was recomputed away')
        entry.write({'amount_override': 0.0})
        self.assertAlmostEqual(entry.amount, 360.0, places=2)

    def test_10_site_specific_tier_wins(self):
        self.env['ksw.pay.rate.tier'].sudo().create({
            'component_id': self.c_trips.id,
            'site_id': self.site.id,
            'name': 'Site flat', 'sequence': 5, 'width': 0.0, 'rate': 2.0,
        })
        batch = self._batch(component=self.c_trips)
        entry = self._entry(batch, quantity=60.0, threshold_qty=50.0)
        # Only the site row applies: 10 earning x 2.00
        self.assertAlmostEqual(entry.amount, 20.0, places=2)


class TestPayBatch(PayEntryCommon):
    """The batch is the supervisor's screen and the unit of submission."""

    def test_01_sequence_and_scope(self):
        batch = self._batch()
        self.assertTrue(batch.name.startswith('PB'))
        self.assertEqual(batch.department_id, self.dept)

    def test_02_department_required_for_department_scope(self):
        with self.assertRaises(ValidationError):
            self.env['ksw.pay.batch'].sudo().create({
                'component_id': self.c_overtime.id, 'period': self.period,
            })

    def test_03_site_required_for_site_scope(self):
        with self.assertRaises(ValidationError):
            self.env['ksw.pay.batch'].sudo().create({
                'component_id': self.c_trips.id, 'period': self.period,
            })

    def test_04_one_batch_per_component_scope_and_month(self):
        self._batch()
        with self.assertRaises(Exception):
            with self.env.cr.savepoint():
                self._batch()

    def test_05_another_component_same_month_is_fine(self):
        self._batch()
        self.assertTrue(self._batch(component=self.c_meals))

    def test_06_totals(self):
        batch = self._batch()
        self._entry(batch, quantity=4.0)
        self._entry(batch, employee=self.emp2, quantity=4.0)
        batch.invalidate_recordset()
        self.assertEqual(batch.entry_count, 2)
        self.assertEqual(batch.employee_count, 2)
        self.assertAlmostEqual(batch.total_quantity, 8.0)
        self.assertAlmostEqual(batch.total_amount, 292.50, places=2)

    def test_07_submit_locks_entries(self):
        batch = self._batch()
        entry = self._entry(batch, quantity=4.0)
        batch.action_submit()
        self.assertEqual(batch.state, 'submitted')
        # Not sudo: the guard exempts env.su for crons and computes.
        admin = self.env.ref('base.user_admin')
        with self.assertRaises(UserError):
            entry.with_user(admin).write({'quantity': 9.0})
        with self.assertRaises(UserError):
            # Not via the helper — that forces sudo(), which the guard
            # exempts on purpose so crons and computes keep working.
            self.env['ksw.pay.entry'].with_user(admin).create({
                'batch_id': batch.id, 'employee_id': self.emp.id,
                'date': self.period, 'quantity': 1.0, 'reason': 'late',
            })

    def test_08_empty_batch_cannot_be_submitted(self):
        with self.assertRaises(UserError):
            self._batch().action_submit()

    def test_09_return_reopens_and_records_the_reason(self):
        batch = self._batch()
        self._entry(batch, quantity=4.0)
        batch.action_submit()
        batch.action_return(reason='Hours look wrong')
        self.assertEqual(batch.state, 'draft')
        self.assertIn('Hours look wrong', batch.return_reason)

    def test_10_date_must_be_inside_the_period(self):
        batch = self._batch()
        with self.assertRaises(ValidationError):
            self._entry(batch, quantity=4.0, date='2028-08-05')

    def test_11_reason_required_when_the_component_says_so(self):
        batch = self._batch()
        with self.assertRaises(ValidationError):
            self.env['ksw.pay.entry'].sudo().create({
                'batch_id': batch.id, 'employee_id': self.emp.id,
                'date': self.period, 'quantity': 4.0,
            })

    def test_12_quantity_must_be_positive(self):
        batch = self._batch()
        with self.assertRaises(ValidationError):
            self._entry(batch, quantity=0.0)

    def test_13_biometric_employee_is_allowed(self):
        """No x_is_attendance_sheet filter: Maintenance is 18/30 biometric."""
        bio = self._employee('Pay Bio', self.dept, 4800.0)
        bio.sudo().write({'x_is_attendance_sheet': False})
        entry = self._entry(self._batch(), employee=bio, quantity=4.0)
        self.assertAlmostEqual(entry.amount, 120.0, places=2)


class TestPayComponentAccess(PayEntryCommon):
    """Which components a supervisor may record is configuration."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.restricted_group = cls.env['res.groups'].sudo().create({
            'name': 'Pay Restricted Test Group',
        })
        cls.user = cls.env['res.users'].sudo().create({
            'name': 'pay_supervisor', 'login': 'pay_supervisor',
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('KSW_commissions.group_commission_supervisor').id,
            ])],
        })
        cls.env['hr.employee'].sudo().create({
            'name': 'pay_supervisor', 'department_id': cls.dept.id,
            'user_id': cls.user.id,
        })

    def test_01_unrestricted_component_is_open_to_any_supervisor(self):
        self.assertTrue(
            self.c_overtime.with_user(self.user)._check_may_enter())

    def test_02_restricted_component_is_refused(self):
        self.c_overtime.sudo().write({
            'entry_group_ids': [(6, 0, [self.restricted_group.id])]})
        self.assertFalse(
            self.c_overtime.with_user(self.user)._check_may_enter())
        with self.assertRaises(UserError):
            self.env['ksw.pay.batch'].with_user(self.user).create({
                'component_id': self.c_overtime.id,
                'department_id': self.dept.id,
                'period': self.period,
            })

    def test_03_restricted_component_allowed_once_in_the_group(self):
        self.c_overtime.sudo().write({
            'entry_group_ids': [(6, 0, [self.restricted_group.id])]})
        self.user.sudo().write({
            'group_ids': [(4, self.restricted_group.id)]})
        self.assertTrue(
            self.c_overtime.with_user(self.user)._check_may_enter())

    def test_04_supervisor_sees_only_the_department_he_manages(self):
        """Scope follows hr.department.manager_id, not the supervisor's own
        department — being *in* a department is not running it."""
        employee = self.env['hr.employee'].sudo().search(
            [('user_id', '=', self.user.id)], limit=1)
        self.dept.sudo().write({'manager_id': employee.id})

        mine = self._batch(dept=self.dept)
        theirs = self._batch(dept=self.other_dept)
        visible = self.env['ksw.pay.batch'].with_user(self.user).search([])
        self.assertIn(mine, visible)
        self.assertNotIn(theirs, visible,
                         "a supervisor must not see another department's batch")

    def test_05_creator_sees_a_site_scoped_batch(self):
        """The create_uid clause carries site-scoped batches, which have no
        department to match the manager rule on."""
        batch = self.env['ksw.pay.batch'].with_user(self.user).create({
            'component_id': self.c_trips.id,
            'site_id': self.site.id,
            'period': self.period,
        })
        visible = self.env['ksw.pay.batch'].with_user(self.user).search([])
        self.assertIn(batch, visible)


class TestPayRecurring(PayEntryCommon):

    def test_01_apply_creates_the_missing_entries(self):
        self.env['ksw.pay.recurring'].sudo().create({
            'employee_id': self.emp.id,
            'component_id': self.c_mobile.id,
            'quantity': 1.0, 'amount': 100.0,
            'date_from': '2028-01-01',
        })
        batch = self._batch(component=self.c_mobile)
        batch.action_add_recurring()
        batch.invalidate_recordset()
        self.assertEqual(batch.entry_count, 1)
        self.assertAlmostEqual(batch.total_amount, 100.0, places=2)

    def test_02_apply_is_idempotent(self):
        self.env['ksw.pay.recurring'].sudo().create({
            'employee_id': self.emp.id,
            'component_id': self.c_mobile.id,
            'quantity': 1.0, 'amount': 100.0,
            'date_from': '2028-01-01',
        })
        batch = self._batch(component=self.c_mobile)
        batch.action_add_recurring()
        batch.action_add_recurring()
        batch.invalidate_recordset()
        self.assertEqual(batch.entry_count, 1,
                         'pressing the button twice must not duplicate')

    def test_03_expired_recurring_is_skipped(self):
        self.env['ksw.pay.recurring'].sudo().create({
            'employee_id': self.emp.id,
            'component_id': self.c_mobile.id,
            'quantity': 1.0, 'amount': 100.0,
            'date_from': '2027-01-01', 'date_to': '2027-12-31',
        })
        batch = self._batch(component=self.c_mobile)
        batch.action_add_recurring()
        batch.invalidate_recordset()
        self.assertEqual(batch.entry_count, 0)


class TestPayEntryUX(PayEntryCommon):
    """The three points raised at review: justify, copy forward, scope."""

    # -- 1. an amount must be able to explain itself -------------------
    def test_01_tiered_explanation_shows_every_band(self):
        batch = self._batch(component=self.c_trips)
        entry = self._entry(batch, quantity=180.0, threshold_qty=50.0,
                            quantity_ref=95.0)
        html = entry.explanation
        self.assertIn('Actual Trips', html,
                      'the raw trip count must be shown for justification')
        self.assertIn('95', html)
        # Free allowance, earning quantity and all four bands.
        self.assertIn('Free allowance', html)
        self.assertIn('Earning quantity', html)
        for tier in ('Tier 2', 'Tier 3', 'Tier 4', 'Tier 5'):
            self.assertIn(tier, html, '%s is missing from the breakdown' % tier)
        # The money per band: 40x10, 40x15, 40x20, 10x25
        for money in ('400.00', '600.00', '800.00', '250.00'):
            self.assertIn(money, html)
        self.assertIn('2050.00', html)

    def test_02_wage_rate_explanation_shows_the_derivation(self):
        entry = self._entry(self._batch(), quantity=4.0)
        html = entry.explanation
        self.assertIn('Basic salary', html)
        self.assertIn('7200', html)
        self.assertIn('240', html)
        self.assertIn('180.00', html)

    def test_03_explanation_notes_an_override(self):
        entry = self._entry(self._batch(), quantity=4.0)
        entry.write({'amount_override': 500.0})
        self.assertIn('overridden', entry.explanation)

    def test_04_action_explain_opens_the_breakdown(self):
        entry = self._entry(self._batch(), quantity=4.0)
        action = entry.action_explain()
        self.assertEqual(action['res_model'], 'ksw.pay.entry')
        self.assertEqual(action['res_id'], entry.id)
        self.assertEqual(action['target'], 'new')

    # -- 2. each new line starts as a copy of the last -----------------
    def test_05_new_line_copies_the_previous_one(self):
        batch = self._batch()
        self._entry(batch, quantity=4.0, date='2028-07-03',
                    reason='Emergency repair')
        Entry = self.env['ksw.pay.entry'].with_context(
            default_batch_id=batch.id)
        defaults = Entry.default_get([
            'employee_id', 'date', 'quantity', 'reason', 'location_id'])
        self.assertEqual(defaults.get('quantity'), 4.0)
        self.assertEqual(defaults.get('reason'), 'Emergency repair')
        self.assertEqual(str(defaults.get('date')), '2028-07-03')
        self.assertEqual(defaults.get('employee_id'), self.emp.id)

    def test_06_first_line_of_a_batch_has_nothing_to_copy(self):
        batch = self._batch()
        defaults = self.env['ksw.pay.entry'].with_context(
            default_batch_id=batch.id).default_get(['quantity', 'reason'])
        self.assertFalse(defaults.get('reason'))

    def test_07_duplicate_line(self):
        batch = self._batch()
        entry = self._entry(batch, quantity=4.0)
        entry.action_duplicate_line()
        batch.invalidate_recordset()
        self.assertEqual(batch.entry_count, 2)
        self.assertAlmostEqual(batch.total_amount, 360.0, places=2)

    # -- 3. a supervisor only reaches departments he runs ---------------
    def _supervisor(self, login, manages=None, assists=None):
        user = self.env['res.users'].sudo().create({
            'name': login, 'login': login,
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref(
                    'KSW_commissions.group_commission_supervisor').id,
            ])],
        })
        employee = self.env['hr.employee'].sudo().create({
            'name': login, 'user_id': user.id,
        })
        for dept in (manages or []):
            dept.sudo().write({'manager_id': employee.id})
        if assists:
            assists.sudo().write({'x_assistant_ids': [(4, user.id)]})
        return user

    def test_08_department_manager_sees_only_his_own(self):
        user = self._supervisor('pay_mgr_a', manages=[self.dept])
        Batch = self.env['ksw.pay.batch'].with_user(user)
        allowed = Batch._allowed_departments()
        self.assertIn(self.dept, allowed)
        self.assertNotIn(self.other_dept, allowed)

    def test_09_single_department_is_chosen_and_locked(self):
        user = self._supervisor('pay_mgr_b', manages=[self.dept])
        Batch = self.env['ksw.pay.batch'].with_user(user)
        defaults = Batch.default_get(['department_id'])
        self.assertEqual(defaults.get('department_id'), self.dept.id,
                         'one option is not a choice — it must be preselected')
        batch = Batch.create({
            'component_id': self.c_overtime.id,
            'department_id': self.dept.id,
            'period': self.period,
        })
        self.assertTrue(batch.department_locked)

    def test_10_two_departments_are_offered_not_locked(self):
        user = self._supervisor('pay_mgr_c',
                                manages=[self.dept, self.other_dept])
        Batch = self.env['ksw.pay.batch'].with_user(user)
        self.assertEqual(len(Batch._allowed_departments()), 2)
        self.assertFalse(Batch.default_get(['department_id']).get(
            'department_id'))

    def test_11_another_department_is_refused_over_rpc(self):
        """The domain narrows the picker; this is what stops an RPC call."""
        user = self._supervisor('pay_mgr_d', manages=[self.dept])
        with self.assertRaises(UserError):
            self.env['ksw.pay.batch'].with_user(user).create({
                'component_id': self.c_overtime.id,
                'department_id': self.other_dept.id,
                'period': self.period,
            })

    def test_12_assistant_reaches_the_manager_s_department(self):
        manager = self._supervisor('pay_mgr_e', manages=[self.dept])
        assistant = self._supervisor('pay_asst_e', assists=manager)
        allowed = self.env['ksw.pay.batch'].with_user(
            assistant)._allowed_departments()
        self.assertIn(self.dept, allowed,
                      "an assistant must reach the manager's department")

    def test_13_a_supervisor_managing_nothing_gets_a_clear_error(self):
        user = self._supervisor('pay_mgr_f')
        self.assertFalse(
            self.env['ksw.pay.batch'].with_user(user)._allowed_departments())
        with self.assertRaises(UserError):
            self.env['ksw.pay.batch'].with_user(user).create({
                'component_id': self.c_overtime.id,
                'department_id': self.dept.id,
                'period': self.period,
            })

    def test_14_officer_still_sees_every_department(self):
        officer = self.env['res.users'].sudo().create({
            'name': 'pay_officer', 'login': 'pay_officer',
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('KSW_commissions.group_commission_officer').id,
            ])],
        })
        allowed = self.env['ksw.pay.batch'].with_user(
            officer)._allowed_departments()
        self.assertIn(self.dept, allowed)
        self.assertIn(self.other_dept, allowed)


class TestComponentOptions(PayEntryCommon):
    """Meals: one component, three choices.

    Breakfast, Lunch and Dinner used to be three components, which meant
    three batches to open and three submissions to make for one day's
    meals. They differ in nothing but the rate — which is what an option
    is — so they are one component now, picked per row.
    """

    def test_01_one_batch_carries_every_meal(self):
        batch = self._batch(component=self.c_meals)
        breakfast = self._entry(batch, quantity=2.0,
                                option_id=self.o_breakfast.id)
        lunch = self._entry(batch, employee=self.emp2, quantity=3.0,
                            option_id=self.o_lunch.id)
        self.assertAlmostEqual(breakfast.amount, 20.0, places=2)
        self.assertAlmostEqual(lunch.amount, 60.0, places=2)
        self.assertAlmostEqual(batch.total_amount, 80.0, places=2)

    def test_02_the_rate_comes_from_the_option(self):
        batch = self._batch(component=self.c_meals)
        entry = self._entry(batch, quantity=1.0, option_id=self.o_lunch.id)
        self.assertAlmostEqual(entry.rate, 20.0, places=2)
        entry.write({'option_id': self.o_breakfast.id})
        self.assertAlmostEqual(entry.rate, 10.0, places=2)
        self.assertAlmostEqual(entry.amount, 10.0, places=2)

    def test_03_a_row_must_say_which_meal(self):
        batch = self._batch(component=self.c_meals)
        with self.assertRaises(ValidationError):
            self.env['ksw.pay.entry'].sudo().create({
                'batch_id': batch.id,
                'employee_id': self.emp.id,
                'quantity': 1.0,
            })

    def test_04_an_option_of_another_component_is_refused(self):
        other = self.env['ksw.pay.option'].sudo().create({
            'component_id': self.env['ksw.pay.component'].sudo().create({
                'name': 'Other qty', 'code': 'OPT_TEST',
                'calculation': 'qty_rate', 'rate': 1.0,
            }).id,
            'name': 'Elsewhere', 'rate': 5.0,
        })
        batch = self._batch(component=self.c_meals)
        with self.assertRaises(ValidationError):
            self._entry(batch, quantity=1.0, option_id=other.id)

    def test_05_a_component_without_options_needs_none(self):
        batch = self._batch(component=self.c_mobile)
        entry = self._entry(batch, quantity=1.0, amount=100.0)
        self.assertFalse(entry.option_id)

    def test_06_options_only_make_sense_on_quantity_times_rate(self):
        component = self.env['ksw.pay.component'].sudo().create({
            'name': 'Fixed with options', 'code': 'OPT_FIXED',
            'calculation': 'fixed',
        })
        with self.assertRaises(ValidationError):
            component.write({
                'option_ids': [(0, 0, {'name': 'A', 'rate': 1.0})],
            })

    def test_07_the_explanation_names_the_meal(self):
        batch = self._batch(component=self.c_meals)
        entry = self._entry(batch, quantity=2.0, option_id=self.o_lunch.id)
        self.assertIn('Lunch', entry.explanation)
        self.assertIn('40.00', entry.explanation)

    def test_08_the_next_line_copies_the_meal(self):
        batch = self._batch(component=self.c_meals)
        self._entry(batch, quantity=2.0, option_id=self.o_breakfast.id)
        defaults = self.env['ksw.pay.entry'].sudo().with_context(
            default_batch_id=batch.id).default_get(
                ['employee_id', 'option_id', 'quantity'])
        self.assertEqual(defaults.get('option_id'), self.o_breakfast.id)

    def test_09_recurring_meals_repeat_per_meal(self):
        Recurring = self.env['ksw.pay.recurring'].sudo()
        for option in (self.o_breakfast, self.o_lunch):
            Recurring.create({
                'employee_id': self.emp.id,
                'component_id': self.c_meals.id,
                'option_id': option.id,
                'quantity': 2.0,
                'date_from': self.period,
            })
        batch = self._batch(component=self.c_meals)
        created = self.env['ksw.pay.recurring']._apply_to_batch(batch)
        self.assertEqual(len(created), 2)
        self.assertEqual(created.mapped('option_id'),
                         self.o_breakfast | self.o_lunch)
        # Idempotent: pressing the button twice adds nothing.
        self.assertFalse(
            self.env['ksw.pay.recurring']._apply_to_batch(batch))
