"""Can the person who records the pay actually see the people he pays?

A supervisor opened a new batch, picked an employee, and the form died with
"you don't have 'read' access to Employee" (KSWCO, 12 Sep 2026). Two separate
defects met there, and there is a class here for each:

1. the picker was computed through ``env.su``, so a batch that had not been
   given a scope yet offered **every employee in the company** — the exact
   trap CLAUDE.md gotcha #42 describes as dormant in this model;

2. even scoped correctly, the picker names whoever the user's *pay* authority
   covers, which is not the same question as his *hr.employee* scope. The
   disagreement is not survivable: reading an x2many back filters it on
   ``active`` in the reader's own environment
   (``Many2many.convert_to_record``), so a single employee outside his HR
   scope raises AccessError for the whole form.

**Every read here is preceded by ``invalidate_all()`` on purpose.** Fixtures
are created with ``sudo()``, which warms the ORM cache for those employees;
a later read as the supervisor then hits the cache and never reaches an
access check. That is exactly why this class of bug reaches production while
the test suite stays green.
"""
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase


class PayEmployeeAccessCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.period = '2029-05-01'
        cls.overtime = env.ref('KSW_commissions.pay_component_overtime')
        cls.trips = env.ref('KSW_commissions.pay_component_driver_trips')

        cls.dept = env['hr.department'].sudo().create({'name': 'Acc Dept'})
        cls.other_dept = env['hr.department'].sudo().create(
            {'name': 'Acc Other Dept'})
        cls.site = env['ksw.site'].sudo().create(
            {'name': 'Acc Site', 'code': 'ACS'})

        cls.supervisor = cls._user(
            'acc_sup', 'KSW_commissions.group_commission_supervisor')
        cls.sup_employee = env['hr.employee'].sudo().create({
            'name': 'Acc Sup', 'user_id': cls.supervisor.id,
            'department_id': cls.dept.id,
        })
        cls.dept.sudo().write({'manager_id': cls.sup_employee.id})

        # His department's staff. Deliberately NOT linked to him by
        # parent_id: that is the normal shape here, and it is what makes the
        # HR scope and the pay scope disagree.
        cls.staff = env['hr.employee'].sudo().create([
            {'name': 'Acc Staff %d' % i, 'department_id': cls.dept.id}
            for i in range(3)
        ])
        # Somebody else's, in every sense.
        cls.outsider = env['hr.employee'].sudo().create({
            'name': 'Acc Outsider', 'department_id': cls.other_dept.id,
        })
        cls.driver = env['hr.employee'].sudo().create({
            'name': 'Acc Driver', 'department_id': cls.other_dept.id,
            'x_site_id': cls.site.id,
        })

        cls.gm = cls._user('acc_gm', 'KSW_commissions.group_commission_gm')
        cls.accountant = cls._user(
            'acc_acct', 'KSW_commissions.group_commission_accountant')
        cls.officer = cls._user(
            'acc_officer', 'KSW_commissions.group_commission_officer')

    @classmethod
    def _user(cls, login, group_xmlid):
        """Every KSW user carries the HR *Employee* group, and that matters.

        Model-level read on hr.employee is what decides whether a user is
        answered by hr.employee or by hr.employee.public (CLAUDE.md gotcha
        #34). A fixture without the group is served by the public model,
        reads every name in the company and never reaches a record rule —
        so it would pass this whole file with the bug still in place.
        """
        return cls.env['res.users'].sudo().create({
            'name': login, 'login': login,
            'group_ids': [(6, 0, [
                cls.env.ref(
                    'KSW_base_security.group_hr_employee_subordinate').id,
                cls.env.ref(group_xmlid).id,
            ])],
        })

    def _batch(self, user=None, **vals):
        vals.setdefault('component_id', self.overtime.id)
        vals.setdefault('period', self.period)
        batch = self.env['ksw.pay.batch'].sudo().create(vals)
        self.env.flush_all()
        self.env.invalidate_all()
        return batch.with_user(user) if user else batch


class TestPickerWidth(PayEmployeeAccessCommon):
    """The picker is the user's authority, not everybody."""

    def test_01_scopeless_batch_does_not_offer_the_company(self):
        """A site-scoped component before its site is chosen.

        This is the state every new Driver Trips batch passes through, and
        it used to fall into the `_allowed_departments()` branch with su
        still on — which returns every department, hence every employee.
        """
        batch = self.env['ksw.pay.batch'].with_user(self.supervisor).new({
            'component_id': self.trips.id, 'period': self.period,
        })
        allowed = batch.allowed_employee_ids
        self.assertNotIn(self.outsider, allowed,
                         "a batch with no scope yet must not offer another "
                         "department's staff")
        self.assertLess(
            len(allowed),
            self.env['hr.employee'].sudo().search_count([]),
            'the fallback picker must never be the whole company')

    def test_02_department_batch_offers_that_department(self):
        batch = self._batch(user=self.supervisor, department_id=self.dept.id)
        allowed = batch.allowed_employee_ids
        for employee in self.staff:
            self.assertIn(employee, allowed)
        self.assertNotIn(self.outsider, allowed)

    def test_03_site_batch_offers_that_site(self):
        batch = self._batch(user=self.supervisor,
                            component_id=self.trips.id, site_id=self.site.id)
        allowed = batch.allowed_employee_ids
        self.assertIn(self.driver, allowed)
        self.assertNotIn(self.outsider, allowed)

    def test_04_a_viewer_gets_no_picker(self):
        """A General Manager reads this form; he never types in it.

        Computing it for him would make the form read employee records his
        role has no hr.employee rule for, which kills the whole web_read.
        """
        batch = self._batch(department_id=self.dept.id)
        for user in (self.gm, self.accountant):
            self.env.invalidate_all()
            self.assertFalse(
                batch.with_user(user).allowed_employee_ids,
                'a read-only role must not be handed an employee picker')


class TestEmployeeReadScope(PayEmployeeAccessCommon):
    """Whoever he may pay, he may read — and nobody else."""

    def test_01_supervisor_reads_his_own_picker(self):
        batch = self._batch(user=self.supervisor, department_id=self.dept.id)
        self.env.invalidate_all()
        try:
            names = batch.allowed_employee_ids.mapped('name')
        except AccessError:
            self.fail('a supervisor must be able to read the employees his '
                      'own batch offers him')
        for employee in self.staff:
            self.assertIn(employee.name, names)

    def test_02_supervisor_reads_a_department_member_directly(self):
        self.env.invalidate_all()
        employee = self.staff[0].with_user(self.supervisor)
        self.assertEqual(employee.name, self.staff[0].name)

    def test_03_supervisor_cannot_read_an_outsider(self):
        self.env.invalidate_all()
        with self.assertRaises(AccessError):
            self.outsider.with_user(self.supervisor).read(['name'])

    def test_04_supervisor_cannot_write_an_employee(self):
        """Read is all this grants — the record itself stays HR's."""
        self.env.invalidate_all()
        with self.assertRaises(AccessError):
            self.staff[0].with_user(self.supervisor).write(
                {'name': 'Renamed by a supervisor'})

    def test_05_the_dropdown_finds_his_staff(self):
        """The picker's domain is only half of it.

        The name search behind the dropdown runs as the user too, so
        without the read rule it comes back empty and the supervisor sees
        an employee list with nobody in it.
        """
        self.env.invalidate_all()
        found = self.env['hr.employee'].with_user(
            self.supervisor).name_search('Acc Staff')
        self.assertEqual(len(found), len(self.staff))

    def test_06_officer_reads_the_company_wide_picker(self):
        batch = self._batch(user=self.officer, department_id=self.dept.id)
        self.env.invalidate_all()
        try:
            batch.allowed_employee_ids.mapped('name')
        except AccessError:
            self.fail("the Administrator's picker is every employee, so his "
                      'read scope has to be too')

    def test_07_the_batch_form_opens_for_everyone_who_may_see_it(self):
        """web_read is what the browser actually calls.

        A read that raises inside an x2many kills the parent record's whole
        payload, which is how this reached the user as a blank form.
        """
        batch = self._batch(department_id=self.dept.id)
        self.env['ksw.pay.entry'].sudo().create({
            'batch_id': batch.id, 'employee_id': self.staff[0].id,
            'quantity': 4.0, 'date': self.period, 'reason': 'access test',
        })
        self.env.flush_all()
        spec = {
            'display_name': {},
            'allowed_employee_ids': {},
            'entry_ids': {'fields': {
                'employee_id': {'fields': {'display_name': {}}},
                'allowed_employee_ids': {},
                'quantity': {}, 'amount': {},
            }},
        }
        for user in (self.supervisor, self.officer):
            self.env.invalidate_all()
            try:
                batch.with_user(user).web_read(spec)
            except AccessError:
                self.fail('%s cannot open a batch form he is allowed to see'
                          % user.login)
