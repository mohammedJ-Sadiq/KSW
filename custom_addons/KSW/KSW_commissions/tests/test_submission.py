"""The department handover — who submits what, and what that freezes.

Three defects motivated this layer, and there is a test here for each:

1. a supervisor could not pull his own submitted batch back to draft, even
   though nobody had looked at it yet;
2. the only submission was the run's, which covers the whole company — so a
   supervisor could not declare himself done without declaring six other
   departments done, and the Monthly Pay Run was unusable to him;
3. nobody could see who was about to be paid until after the month was
   approved, which is precisely the wrong order for a review.

The fourth class of test here guards the repair of a real data-loss bug: the
register preview must never touch a line it did not create.
"""
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class SubmissionCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.period = '2029-03-01'
        cls.component = env.ref('KSW_commissions.pay_component_overtime')
        cls.meals = env.ref('KSW_commissions.pay_component_meals')
        cls.o_lunch = env.ref('KSW_commissions.pay_option_meal_lunch')

        cls.dept_a = env['hr.department'].create({'name': 'Sub Dept A'})
        cls.dept_b = env['hr.department'].create({'name': 'Sub Dept B'})

        cls.sup_a = cls._supervisor('sub_sup_a', cls.dept_a)
        cls.sup_b = cls._supervisor('sub_sup_b', cls.dept_b)
        cls.gm = cls._user('sub_gm', 'KSW_commissions.group_commission_gm')
        # Approval follows hr.department.x_effective_gm_id now, not the GM
        # group, so this fixture's GM has to be named on both departments to
        # keep the "one GM over everything" shape these tests assume. See
        # tests/test_department_gm.py for the per-department contract.
        cls.gm_employee = cls.env['hr.employee'].sudo().create({
            'name': 'Sub GM Emp', 'user_id': cls.gm.id})
        (cls.dept_a | cls.dept_b).sudo().write(
            {'x_gm_id': cls.gm_employee.id})

        cls.emp_a = cls._employee('Sub Emp A', cls.dept_a, 7200.0)
        cls.emp_b = cls._employee('Sub Emp B', cls.dept_b, 4800.0)

    # -- fixtures -------------------------------------------------------
    @classmethod
    def _user(cls, login, group_xmlid):
        return cls.env['res.users'].sudo().create({
            'name': login, 'login': login,
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref(group_xmlid).id,
            ])],
        })

    @classmethod
    def _supervisor(cls, login, department):
        """A supervisor is the manager of his department — the one link the
        whole scoping hangs off (and the deployment prerequisite)."""
        user = cls._user(
            login, 'KSW_commissions.group_commission_supervisor')
        employee = cls.env['hr.employee'].sudo().create({
            'name': login, 'user_id': user.id, 'department_id': department.id,
        })
        department.sudo().write({'manager_id': employee.id})
        return user

    @classmethod
    def _employee(cls, name, dept, wage):
        emp = cls.env['hr.employee'].sudo().create({
            'name': name, 'department_id': dept.id,
        })
        if emp.current_version_id:
            emp.current_version_id.sudo().write({'wage': wage})
        return emp

    def _batch(self, user, department, component=None, period=None):
        component = component or self.component
        return self.env['ksw.pay.batch'].with_user(user).create({
            'component_id': component.id,
            'department_id': department.id,
            'period': period or self.period,
        })

    def _entry(self, batch, employee, user=None, **kwargs):
        vals = {'batch_id': batch.id, 'employee_id': employee.id,
                'quantity': 4.0, 'reason': 'probe'}
        if batch.component_id.needs_date:
            vals['date'] = batch.period
        if batch.component_id.has_options:
            vals['option_id'] = batch.component_id.option_ids[0].id
        vals.update(kwargs)
        model = self.env['ksw.pay.entry']
        return (model.with_user(user) if user else model.sudo()).create(vals)

    def _filled_batch(self, user, department, employee, **kwargs):
        batch = self._batch(user, department, **kwargs)
        self._entry(batch, employee, user=user)
        return batch


class TestBatchReopen(SubmissionCommon):
    """Defect 1 — a supervisor could not undo his own submission."""

    def test_01_supervisor_reopens_his_own_submitted_batch(self):
        batch = self._filled_batch(self.sup_a, self.dept_a, self.emp_a)
        batch.with_user(self.sup_a).action_submit()
        self.assertEqual(batch.state, 'submitted')
        self.assertTrue(batch.with_user(self.sup_a).x_can_reopen)

        batch.with_user(self.sup_a).action_reset_to_draft()
        self.assertEqual(batch.state, 'draft')

    def test_02_and_can_edit_its_entries_again(self):
        batch = self._filled_batch(self.sup_a, self.dept_a, self.emp_a)
        batch.with_user(self.sup_a).action_submit()
        batch.with_user(self.sup_a).action_reset_to_draft()
        batch.entry_ids.with_user(self.sup_a).write({'quantity': 6.0})
        self.assertEqual(batch.entry_ids[0].quantity, 6.0)

    def test_03_frozen_once_the_department_is_handed_over(self):
        batch = self._filled_batch(self.sup_a, self.dept_a, self.emp_a)
        batch.submission_id.with_user(self.sup_a).action_submit()
        self.assertFalse(batch.with_user(self.sup_a).x_can_reopen)
        with self.assertRaises(UserError):
            batch.with_user(self.sup_a).action_reset_to_draft()

    def test_04_but_he_may_take_the_submission_back(self):
        """Nobody has looked yet, so this needs no one's permission."""
        batch = self._filled_batch(self.sup_a, self.dept_a, self.emp_a)
        submission = batch.submission_id
        submission.with_user(self.sup_a).action_submit()
        submission.with_user(self.sup_a).action_reset_to_draft()
        self.assertEqual(submission.state, 'draft')
        batch.with_user(self.sup_a).action_reset_to_draft()
        self.assertEqual(batch.state, 'draft')

    def test_05_the_gm_can_always_reopen(self):
        batch = self._filled_batch(self.sup_a, self.dept_a, self.emp_a)
        batch.submission_id.with_user(self.sup_a).action_submit()
        batch.with_user(self.gm).action_reset_to_draft()
        self.assertEqual(batch.state, 'draft')


class TestDepartmentScope(SubmissionCommon):
    """Defect 2 — the month is shared, the responsibility is not."""

    def test_10_a_batch_creates_its_month_and_its_submission(self):
        batch = self._filled_batch(self.sup_a, self.dept_a, self.emp_a)
        self.assertTrue(batch.submission_id)
        self.assertEqual(batch.submission_id.department_id, self.dept_a)
        self.assertTrue(batch.submission_id.run_id)
        self.assertEqual(str(batch.submission_id.run_id.period), self.period)

    def test_11_one_submission_per_department_not_per_batch(self):
        first = self._filled_batch(self.sup_a, self.dept_a, self.emp_a)
        second = self._filled_batch(
            self.sup_a, self.dept_a, self.emp_a, component=self.meals)
        self.assertEqual(first.submission_id, second.submission_id)
        self.assertEqual(first.submission_id.batch_count, 2)

    def test_12_submitting_mine_leaves_yours_alone(self):
        mine = self._filled_batch(self.sup_a, self.dept_a, self.emp_a)
        yours = self._filled_batch(self.sup_b, self.dept_b, self.emp_b)

        run = mine.submission_id.run_id
        run.with_user(self.sup_a).action_submit_my_departments()

        self.assertEqual(mine.submission_id.state, 'submitted')
        self.assertEqual(mine.state, 'submitted')
        self.assertEqual(yours.submission_id.state, 'draft')
        self.assertEqual(yours.state, 'draft')

    def test_13_a_supervisor_cannot_submit_another_department(self):
        yours = self._filled_batch(self.sup_b, self.dept_b, self.emp_b)
        with self.assertRaises(UserError):
            yours.submission_id.with_user(self.sup_a).action_submit()

    def test_14_and_does_not_even_see_it(self):
        self._filled_batch(self.sup_a, self.dept_a, self.emp_a)
        yours = self._filled_batch(self.sup_b, self.dept_b, self.emp_b)
        visible = self.env['ksw.pay.submission'].with_user(
            self.sup_a).search([('period', '=', self.period)])
        self.assertIn(self.dept_a, visible.mapped('department_id'))
        self.assertNotIn(yours.submission_id, visible)

    def test_15_the_month_follows_its_departments(self):
        mine = self._filled_batch(self.sup_a, self.dept_a, self.emp_a)
        yours = self._filled_batch(self.sup_b, self.dept_b, self.emp_b)
        run = mine.submission_id.run_id

        mine.submission_id.with_user(self.sup_a).action_submit()
        self.assertEqual(run.state, 'open')
        self.assertEqual(run.submitted_department_count, 1)
        self.assertEqual(run.pending_department_count, 1)

        yours.submission_id.with_user(self.sup_b).action_submit()
        self.assertEqual(run.state, 'submitted')
        self.assertEqual(run.pending_department_count, 0)

    def test_16_gm_returns_with_a_reason_and_it_reaches_the_batches(self):
        batch = self._filled_batch(self.sup_a, self.dept_a, self.emp_a)
        submission = batch.submission_id
        submission.with_user(self.sup_a).action_submit()

        # A return with no reason tells the supervisor nothing.
        with self.assertRaises(UserError):
            submission.with_user(self.gm).action_return()

        submission.with_user(self.gm).write({'return_reason': 'Fix the hours'})
        submission.with_user(self.gm).action_return()

        self.assertEqual(submission.state, 'returned')
        self.assertEqual(batch.state, 'draft')
        self.assertEqual(batch.return_reason, 'Fix the hours')
        batch.entry_ids.with_user(self.sup_a).write({'quantity': 2.0})

    def test_17_only_the_gm_returns(self):
        batch = self._filled_batch(self.sup_a, self.dept_a, self.emp_a)
        batch.submission_id.with_user(self.sup_a).action_submit()
        batch.submission_id.sudo().write({'return_reason': 'no'})
        with self.assertRaises(UserError):
            batch.submission_id.with_user(self.sup_b).action_return()


class TestRegisterPreview(SubmissionCommon):
    """Defect 3 — who gets paid has to be visible before approval."""

    def test_20_the_register_exists_before_approval(self):
        batch = self._filled_batch(self.sup_a, self.dept_a, self.emp_a)
        run = batch.submission_id.run_id
        self.assertFalse(run.line_ids)

        batch.submission_id.with_user(self.sup_a).action_submit()
        line = run.line_ids.filtered(lambda l: l.employee_id == self.emp_a)
        self.assertTrue(line, "the register must be built at handover")
        self.assertAlmostEqual(line.earnings, 180.0, places=2)
        self.assertTrue(line.is_preview)

    def test_21_a_draft_department_is_not_in_it(self):
        self._filled_batch(self.sup_b, self.dept_b, self.emp_b)
        batch = self._filled_batch(self.sup_a, self.dept_a, self.emp_a)
        batch.submission_id.with_user(self.sup_a).action_submit()
        run = batch.submission_id.run_id
        self.assertNotIn(self.emp_b, run.line_ids.mapped('employee_id'))

    def test_22_a_supervisor_sees_only_his_own_people(self):
        a = self._filled_batch(self.sup_a, self.dept_a, self.emp_a)
        b = self._filled_batch(self.sup_b, self.dept_b, self.emp_b)
        a.submission_id.with_user(self.sup_a).action_submit()
        b.submission_id.with_user(self.sup_b).action_submit()
        run = a.submission_id.run_id

        self.assertEqual(len(run.line_ids), 2)
        visible = run.with_user(self.sup_a).x_visible_line_ids
        self.assertEqual(visible.mapped('employee_id'), self.emp_a)

    def test_23_the_gm_sees_every_department(self):
        a = self._filled_batch(self.sup_a, self.dept_a, self.emp_a)
        b = self._filled_batch(self.sup_b, self.dept_b, self.emp_b)
        a.submission_id.with_user(self.sup_a).action_submit()
        b.submission_id.with_user(self.sup_b).action_submit()
        run = a.submission_id.run_id

        seen = run.with_user(self.gm).x_visible_line_ids
        self.assertEqual(len(seen), 2)
        depts = run.with_user(self.gm).x_visible_submission_ids
        self.assertEqual(set(depts.mapped('department_id')),
                         {self.dept_a, self.dept_b})
        self.assertAlmostEqual(sum(depts.mapped('total_amount')),
                               sum(run.line_ids.mapped('earnings')), places=2)

    def test_24_preview_follows_a_correction(self):
        batch = self._filled_batch(self.sup_a, self.dept_a, self.emp_a)
        submission = batch.submission_id
        run = submission.run_id
        submission.with_user(self.sup_a).action_submit()
        self.assertAlmostEqual(run.line_ids.earnings, 180.0, places=2)

        submission.with_user(self.sup_a).action_reset_to_draft()
        self.assertFalse(run.line_ids,
                         "a withdrawn department leaves the register")

    def test_25_preview_never_touches_a_settled_line(self):
        """The data-loss guard.

        A run can already carry settled figures — the migration rebuilt the
        historical commission sheets as register lines. Rebuilding a
        projection is no reason to delete them, and an earlier version of
        this preview did exactly that.
        """
        batch = self._filled_batch(self.sup_a, self.dept_a, self.emp_a)
        run = batch.submission_id.run_id
        settled = self.env['ksw.pay.run.line'].sudo().create({
            'run_id': run.id, 'employee_id': self.emp_b.id,
            'earnings': 999.0,
        })
        self.assertFalse(settled.x_preview_generated)

        batch.submission_id.with_user(self.sup_a).action_submit()

        self.assertTrue(settled.exists(), "a settled line must survive")
        self.assertAlmostEqual(settled.earnings, 999.0, places=2)


class TestBlankForm(SubmissionCommon):
    """A supervisor who runs exactly one department gets it preselected.

    Driven through ``onchange()`` the way the web client does it, because
    that is where this broke and a plain ``default_get`` probe cannot see it
    (CLAUDE.md gotcha #39: verifying view-layer behaviour from the shell is
    misleading unless you drive the same call the client drives).
    """

    SPEC = {
        'component_id': {}, 'period': {}, 'department_id': {},
        'site_id': {}, 'department_locked': {}, 'scope': {}, 'state': {},
        'allowed_department_ids': {'fields': {}},
    }

    def _department_of(self, value):
        got = value.get('department_id')
        return got['id'] if isinstance(got, dict) else got

    def test_40_a_blank_form_keeps_its_preselected_department(self):
        """The regression: the field was locked AND empty, so the required
        error had no answer. Odoo runs the onchanges once on a blank form,
        and the component is not chosen yet at that point."""
        batch = self.env['ksw.pay.batch'].with_user(self.sup_a)
        value = batch.onchange({}, [], self.SPEC)['value']
        self.assertTrue(value.get('department_locked'),
                        "one department means it must be locked")
        self.assertEqual(self._department_of(value), self.dept_a.id,
                         "a locked field must not be left empty")

    def test_41_choosing_a_component_keeps_it(self):
        batch = self.env['ksw.pay.batch'].with_user(self.sup_a)
        value = batch.onchange({}, [], self.SPEC)['value']
        value = dict(value, component_id=self.component.id)
        value.pop('allowed_department_ids', None)
        after = batch.onchange(value, ['component_id'], self.SPEC)['value']
        self.assertNotIn('department_id', after,
                         "a department-scoped component must not clear it")

    def test_42_switching_scope_and_back_restores_it(self):
        trips = self.env.ref('KSW_commissions.pay_component_driver_trips')
        batch = self.env['ksw.pay.batch'].with_user(self.sup_a)
        value = batch.onchange({}, [], self.SPEC)['value']
        value.pop('allowed_department_ids', None)

        value = dict(value, component_id=trips.id)
        after = batch.onchange(value, ['component_id'], self.SPEC)['value']
        self.assertFalse(self._department_of(after),
                         "a site-scoped component has no department")

        value = dict(value, component_id=self.component.id,
                     department_id=False)
        back = batch.onchange(value, ['component_id'], self.SPEC)['value']
        self.assertEqual(self._department_of(back), self.dept_a.id,
                         "switching back must restore the preselection")


class TestEmployeeScope(SubmissionCommon):
    """The employee picker must not be wider than the supervisor's reach."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        boss = cls.env['hr.employee'].sudo().search(
            [('user_id', '=', cls.sup_a.id)], limit=1)
        # A team leader who sits in another department but reports to A,
        # and one of *his* people — the cascade this has to reach.
        cls.lead = cls.env['hr.employee'].sudo().create({
            'name': 'Sub Lead', 'department_id': cls.dept_b.id,
            'parent_id': boss.id,
        })
        cls.deep = cls.env['hr.employee'].sudo().create({
            'name': 'Sub Deep', 'department_id': cls.dept_b.id,
            'parent_id': cls.lead.id,
        })

    def test_50_the_picker_is_the_batch_scope_plus_my_chain(self):
        batch = self._batch(self.sup_a, self.dept_a)
        allowed = batch.with_user(self.sup_a).allowed_employee_ids
        self.assertIn(self.emp_a, allowed, "his own department")
        self.assertIn(self.lead, allowed, "a direct report elsewhere")
        self.assertIn(self.deep, allowed, "and cascading below him")
        self.assertNotIn(self.emp_b, allowed,
                         "another department's staff must not be offered")

    def test_51_a_supervisor_cannot_record_for_someone_elses_staff(self):
        batch = self._batch(self.sup_a, self.dept_a)
        with self.assertRaises(UserError):
            self._entry(batch, self.emp_b, user=self.sup_a)

    def test_52_nor_by_editing_an_entry_afterwards(self):
        batch = self._batch(self.sup_a, self.dept_a)
        entry = self._entry(batch, self.emp_a, user=self.sup_a)
        with self.assertRaises(UserError):
            entry.with_user(self.sup_a).write({'employee_id': self.emp_b.id})

    def test_53_but_may_record_for_a_subordinate_elsewhere(self):
        batch = self._batch(self.sup_a, self.dept_a)
        entry = self._entry(batch, self.deep, user=self.sup_a)
        self.assertEqual(entry.employee_id, self.deep)

    def test_54_the_other_supervisor_sees_his_own(self):
        batch = self._batch(self.sup_b, self.dept_b)
        allowed = batch.with_user(self.sup_b).allowed_employee_ids
        self.assertIn(self.emp_b, allowed)
        self.assertNotIn(self.emp_a, allowed)

    def test_55_an_officer_is_not_narrowed(self):
        officer = self._user('sub_officer',
                             'KSW_commissions.group_commission_officer')
        batch = self._batch(self.sup_a, self.dept_a)
        allowed = batch.with_user(officer).allowed_employee_ids
        self.assertIn(self.emp_a, allowed)
        self.env['ksw.pay.entry'].with_user(officer).create({
            'batch_id': batch.id, 'employee_id': self.emp_b.id,
            'date': batch.period, 'quantity': 1.0, 'reason': 'company-wide',
        })

    def test_56_a_site_batch_follows_the_site(self):
        """Driver trips are recorded per work site — and that is exactly the
        set the BAS import fills in, so the picker has to match it."""
        site = self.env['ksw.site'].sudo().create(
            {'name': 'Sub Site', 'code': 'SUBS'})
        driver = self._employee('Sub Driver', self.dept_b, 3000.0)
        driver.sudo().write({'x_site_id': site.id})
        trips = self.env.ref('KSW_commissions.pay_component_driver_trips')
        batch = self.env['ksw.pay.batch'].with_user(self.sup_a).create({
            'component_id': trips.id, 'site_id': site.id,
            'period': self.period,
        })
        # default_get preselects the supervisor's department on every new
        # batch; a site-scoped one must not keep it, or it draws its
        # employee list from the wrong place.
        self.assertFalse(batch.department_id)
        allowed = batch.with_user(self.sup_a).allowed_employee_ids
        self.assertIn(driver, allowed)
        self.assertNotIn(self.emp_b, allowed)


class TestApproval(SubmissionCommon):

    def _submit(self, *batches):
        for batch in batches:
            batch.submission_id.sudo().action_submit()
        return batches[0].submission_id.run_id

    def test_30_approval_pays_only_what_was_handed_over(self):
        mine = self._filled_batch(self.sup_a, self.dept_a, self.emp_a)
        yours = self._filled_batch(self.sup_b, self.dept_b, self.emp_b)
        run = self._submit(mine)

        run.with_user(self.gm).action_approve()

        self.assertEqual(run.state, 'approved')
        self.assertEqual(mine.state, 'approved')
        self.assertEqual(yours.state, 'draft')
        self.assertEqual(run.line_ids.mapped('employee_id'), self.emp_a)
        self.assertFalse(run.line_ids.is_preview)

    def test_31_nothing_handed_over_means_nothing_to_approve(self):
        batch = self._filled_batch(self.sup_a, self.dept_a, self.emp_a)
        run = batch.submission_id.run_id
        with self.assertRaises(UserError):
            run.with_user(self.gm).action_approve()

    def test_32_only_the_gm_approves(self):
        batch = self._filled_batch(self.sup_a, self.dept_a, self.emp_a)
        run = self._submit(batch)
        with self.assertRaises(UserError):
            run.with_user(self.sup_a).action_approve()

    def test_33_reopening_puts_the_departments_back(self):
        batch = self._filled_batch(self.sup_a, self.dept_a, self.emp_a)
        run = self._submit(batch)
        run.with_user(self.gm).action_approve()

        run.sudo().action_reopen()

        self.assertEqual(run.state, 'submitted')
        self.assertEqual(batch.submission_id.state, 'submitted')
        self.assertTrue(run.line_ids, "the register stays as a preview")
        self.assertAlmostEqual(run.line_ids.earnings, 180.0, places=2)
