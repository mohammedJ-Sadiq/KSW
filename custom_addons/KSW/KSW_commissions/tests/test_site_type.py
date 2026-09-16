"""Work sites are overtime locations; the trip calculation is not a place.

Driver Trips used to be recorded per work site, which meant the batch
asked a supervisor to pick one from a register of 18 places that existed
almost entirely to be named on *overtime* entries. It is recorded per
department now — the department he already runs — and the one figure the
site used to supply (the required-trips base) lives on a single hidden
settings record instead.

What is worth pinning is therefore: the trips component no longer asks
for a site at all, the drivers come from the department, the allowance
comes from the settings record, and that record never surfaces in a
picker or a list.
"""
from unittest.mock import patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase
from odoo.tools.safe_eval import safe_eval


class TestSiteType(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.settings = env['ksw.site']._trip_settings()
        cls.location = env['ksw.site'].create({
            'name': 'Overtime Place', 'code': 'OP',
        })
        cls.c_trips = env.ref('KSW_commissions.pay_component_driver_trips')
        cls.c_overtime = env.ref('KSW_commissions.pay_component_overtime')

    # ------------------------------------------------------------------
    # The register
    # ------------------------------------------------------------------
    def test_the_settings_record_exists_and_is_the_only_one(self):
        self.assertTrue(self.settings, 'the Default record must be seeded')
        self.assertEqual(self.settings.site_type, 'calculation')
        self.assertEqual(
            self.env['ksw.site'].search_count(
                [('site_type', '=', 'calculation')]), 1)

    def test_a_new_site_is_a_location(self):
        """Anything a user creates is a place, never the calculation."""
        self.assertEqual(self.location.site_type, 'location')

    def test_a_second_calculation_record_is_refused(self):
        with self.assertRaises(ValidationError):
            self.env['ksw.site'].create({
                'name': 'Second Default', 'site_type': 'calculation',
            })

    def test_the_settings_record_cannot_be_archived(self):
        with self.assertRaises(ValidationError):
            self.settings.active = False

    # ------------------------------------------------------------------
    # It stays out of sight
    # ------------------------------------------------------------------
    def _sites_matching(self, domain):
        return self.env['ksw.site'].search(safe_eval(domain))

    def test_work_sites_action_excludes_the_settings_record(self):
        """Configuration -> Work Sites is places, and only places."""
        action = self.env.ref('KSW_commissions.action_ksw_site')
        sites = self._sites_matching(action.domain)
        self.assertIn(self.location, sites)
        self.assertNotIn(self.settings, sites)

    def test_location_picker_excludes_the_settings_record(self):
        """"Doesn't appear in search at all" — including the OT picker."""
        sites = self._sites_matching(
            self.env['ksw.pay.entry']._fields['location_id'].domain)
        self.assertIn(self.location, sites)
        self.assertNotIn(self.settings, sites)

    # ------------------------------------------------------------------
    # Driver Trips asks for a department, not a site
    # ------------------------------------------------------------------
    def test_trips_is_department_scoped(self):
        self.assertEqual(self.c_trips.scope, 'department')

    def test_site_is_no_longer_a_scope_anything_can_be_given(self):
        """The option is gone, so the picker cannot come back by config."""
        self.assertNotIn(
            'site',
            dict(self.env['ksw.pay.component']._fields['scope'].selection))

    def test_a_trips_batch_without_a_department_is_refused(self):
        with self.assertRaises(ValidationError):
            self.env['ksw.pay.batch'].create({
                'component_id': self.c_trips.id, 'period': '2028-07-01',
            })

    def test_a_new_batch_never_carries_a_site(self):
        dept = self.env['hr.department'].create({'name': 'Trips Dept'})
        batch = self.env['ksw.pay.batch'].create({
            'component_id': self.c_trips.id, 'period': '2028-07-01',
            'department_id': dept.id,
        })
        self.assertFalse(batch.site_id)


class TestTripImportFollowsTheDepartment(TransactionCase):
    """The drivers are whoever is under the batch's department."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.c_trips = env.ref('KSW_commissions.pay_component_driver_trips')
        cls.mine = env['hr.department'].create({'name': 'My Drivers'})
        cls.theirs = env['hr.department'].create({'name': 'Other Drivers'})
        cls.driver = cls._driver('Mine', cls.mine, 'CC MINE1')
        cls.outsider = cls._driver('Theirs', cls.theirs, 'CC THEIRS1')
        cls.rows = {
            'cc mine1': {'loads': 10, 'mult': 20.0,
                         'equip': 'T1', 'raw_cc': 'CC MINE1'},
            'cc theirs1': {'loads': 99, 'mult': 99.0,
                           'equip': 'T2', 'raw_cc': 'CC THEIRS1'},
        }

    @classmethod
    def _driver(cls, name, dept, cost_center):
        return cls.env['hr.employee'].sudo().create({
            'name': name, 'department_id': dept.id,
            'x_bas_driver_cost_center': cost_center,
        })

    def _import(self, batch):
        with patch.object(
            type(batch), '_bas_fetch_orood', return_value=self.rows
        ):
            return batch._import_bas_trips()

    def test_import_takes_the_department_and_not_another(self):
        """BAS has both drivers; only the department's one is imported.

        The negative half is the one that matters — the positive half
        passed just as well when the pool was every employee in the
        company.
        """
        batch = self.env['ksw.pay.batch'].sudo().create({
            'component_id': self.c_trips.id, 'period': '2028-07-01',
            'department_id': self.mine.id,
        })
        self._import(batch)

        paid = batch.entry_ids.mapped('employee_id')
        self.assertIn(self.driver, paid)
        self.assertNotIn(self.outsider, paid)

    def test_allowance_comes_from_the_settings_record(self):
        settings = self.env['ksw.site']._trip_settings()
        settings.sudo().required_trips_full_month = 44
        batch = self.env['ksw.pay.batch'].sudo().create({
            'component_id': self.c_trips.id, 'period': '2028-07-01',
            'department_id': self.mine.id,
        })
        self._import(batch)

        # No attendance sheet for this employee, so the full-month base.
        self.assertAlmostEqual(
            batch.entry_ids.threshold_qty, 44.0, places=2)


class TestImportOnlyEntries(TransactionCase):
    """Driver Trips is imported and reviewed, never typed.

    The supervisor presses Import and reads the result. A wrong figure
    means a wrong cost centre or wrong data in BAS — both fixed there and
    re-imported — so there is nothing in the batch he is in a position to
    correct, and the tab does not pretend otherwise.

    The read-only Entries tab is cosmetic; every assertion here goes
    through the ORM, which is the path an RPC call takes.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.c_trips = env.ref('KSW_commissions.pay_component_driver_trips')
        cls.c_overtime = env.ref('KSW_commissions.pay_component_overtime')
        cls.dept = env['hr.department'].create({'name': 'Import Only Dept'})
        cls.emp = env['hr.employee'].sudo().create({
            'name': 'Trip Driver', 'department_id': cls.dept.id,
            'x_bas_driver_cost_center': 'CC LOCK1',
        })
        # A supervisor, because env.su is exempt from the guard by design
        # and would make every one of these tests pass for the wrong reason.
        cls.user = env['res.users'].sudo().create({
            'name': 'lock_sup', 'login': 'lock_sup',
            'group_ids': [(6, 0, [
                env.ref('base.group_user').id,
                env.ref('KSW_commissions.group_commission_supervisor').id,
            ])],
        })
        cls.sup_employee = env['hr.employee'].sudo().create({
            'name': 'lock_sup', 'department_id': cls.dept.id,
            'user_id': cls.user.id,
        })
        cls.dept.sudo().manager_id = cls.sup_employee

    def setUp(self):
        super().setUp()
        # Built per test, never shared on the class: TransactionCase rolls
        # back the database between tests, not Python state, so a class
        # attribute mutated by one test (the re-import one does exactly
        # that) silently changes what the next one is asserting.
        self.rows = {'cc lock1': {'loads': 60, 'mult': 60.0,
                                  'equip': 'T9', 'raw_cc': 'CC LOCK1'}}

    def _batch(self, component=None):
        return self.env['ksw.pay.batch'].with_user(self.user).create({
            'component_id': (component or self.c_trips).id,
            'period': '2028-07-01',
            'department_id': self.dept.id,
        })

    def _import(self, batch):
        with patch.object(
            type(batch), '_bas_fetch_orood', return_value=self.rows
        ):
            return batch.action_import()

    # ------------------------------------------------------------------
    def test_the_component_is_flagged_import_only(self):
        self.assertTrue(self.c_trips.entries_import_only)
        self.assertFalse(self.c_overtime.entries_import_only)

    def test_the_supervisor_cannot_add_a_line(self):
        batch = self._batch()
        with self.assertRaises(UserError):
            self.env['ksw.pay.entry'].with_user(self.user).create({
                'batch_id': batch.id, 'employee_id': self.emp.id,
                'quantity': 10.0,
            })

    def test_the_supervisor_cannot_edit_an_imported_line(self):
        batch = self._batch()
        self._import(batch)
        entry = batch.entry_ids
        self.assertTrue(entry, 'the import must have produced a line')
        with self.assertRaises(UserError):
            entry.with_user(self.user).write({'quantity': 999.0})

    def test_the_supervisor_cannot_override_the_amount(self):
        """The override is the obvious way round a locked quantity."""
        batch = self._batch()
        self._import(batch)
        with self.assertRaises(UserError):
            batch.entry_ids.with_user(self.user).write(
                {'amount_override': 5000.0})

    def test_the_supervisor_cannot_delete_an_imported_line(self):
        batch = self._batch()
        self._import(batch)
        with self.assertRaises(UserError):
            batch.entry_ids.with_user(self.user).unlink()

    def test_the_supervisor_cannot_duplicate_a_line(self):
        batch = self._batch()
        self._import(batch)
        with self.assertRaises(UserError):
            batch.entry_ids.with_user(self.user).action_duplicate_line()

    def test_the_import_itself_still_works(self):
        """The guard must not lock out the one way in.

        The import goes through the same create/write path, so this is the
        half that a careless guard breaks.
        """
        batch = self._batch()
        self._import(batch)
        self.assertEqual(len(batch.entry_ids), 1)
        self.assertAlmostEqual(batch.entry_ids.quantity, 60.0, places=2)

    def test_reimporting_still_updates_in_place(self):
        """A correction in BAS reaches the batch by re-importing."""
        batch = self._batch()
        self._import(batch)
        self.rows['cc lock1']['mult'] = 75.0
        self._import(batch)
        self.assertEqual(len(batch.entry_ids), 1)
        self.assertAlmostEqual(batch.entry_ids.quantity, 75.0, places=2)

    def test_another_component_is_untouched(self):
        """Only the flagged component is locked; overtime is still typed."""
        batch = self._batch(component=self.c_overtime)
        entry = self.env['ksw.pay.entry'].with_user(self.user).create({
            'batch_id': batch.id, 'employee_id': self.emp.id,
            'date': '2028-07-05', 'quantity': 3.0,
            'reason': 'stock count',   # Overtime requires one
        })
        self.assertTrue(entry.exists())
        entry.with_user(self.user).write({'quantity': 4.0})
        self.assertAlmostEqual(entry.quantity, 4.0, places=2)

    def test_the_view_gate_follows_the_user(self):
        batch = self._batch()
        self.assertTrue(batch.with_user(self.user).x_entries_readonly)
        self.env.invalidate_all()
        admin = self.env.ref('base.user_admin')
        self.assertFalse(batch.with_user(admin).x_entries_readonly,
                         'somebody has to be able to intervene')

    def test_import_only_needs_an_importer(self):
        """Otherwise the component is a dead end with nothing to explain it."""
        with self.assertRaises(ValidationError):
            self.env['ksw.pay.component'].create({
                'name': 'Dead End', 'code': 'DEADEND',
                'kind': 'earning', 'calculation': 'fixed',
                'entries_import_only': True,
            })
