"""A supervisor owns his own team's recurring pay entries (19.0.3.5.0).

Recurring entries used to be the Officer's alone — the supervisor had read
access and a menu he could not reach, so the standing instructions for his own
team were somebody else's to type. Opening that up means the scope has to hold
in three places at once, and each of them is a test here:

* the **picker** (``allowed_employee_ids``) — never wider than his authority;
* the **guard** (``_check_employee_allowed``) — because a narrowed picker is
  cosmetic against an RPC call;
* the **record rule** — what he can see at all.

The fourth test class covers the one place the rule must deliberately be
bypassed: ``_apply_to_batch`` searches with ``sudo()``, because the rule matches
a direct ``parent_id`` while the batch's own scope walks the whole chain down.
"""
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

from .test_pay_entry import PayEntryCommon


class RecurringAccessCommon(PayEntryCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.supervisor = cls._user(
            'rec_supervisor', 'KSW_commissions.group_commission_supervisor')
        cls.sup_employee = cls.env['hr.employee'].sudo().create({
            'name': 'rec_supervisor', 'department_id': cls.dept.id,
            'user_id': cls.supervisor.id,
        })
        cls.dept.sudo().write({'manager_id': cls.sup_employee.id})

        cls.officer = cls._user(
            'rec_officer', 'KSW_commissions.group_commission_officer')

        # Somebody else's staff: another department, no reporting line here.
        cls.outsider = cls._employee('Rec Outsider', cls.other_dept, 5000.0)

    @classmethod
    def _user(cls, login, group_xmlid):
        return cls.env['res.users'].sudo().create({
            'name': login, 'login': login,
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref(group_xmlid).id,
            ])],
        })

    def _recurring_vals(self, employee, component=None, **kwargs):
        component = component or self.c_mobile
        vals = {
            'employee_id': employee.id,
            'component_id': component.id,
            'amount': 150.0,
            'date_from': self.period,
        }
        if component.has_options:
            vals['option_id'] = component.option_ids[0].id
        vals.update(kwargs)
        return vals


class TestRecurringSupervisorScope(RecurringAccessCommon):

    def test_01_supervisor_creates_for_his_own_department(self):
        rec = self.env['ksw.pay.recurring'].with_user(self.supervisor).create(
            self._recurring_vals(self.emp))
        self.assertEqual(rec.employee_id, self.emp)
        self.assertEqual(rec.department_id, self.dept)

    def test_02_supervisor_creates_for_a_direct_report(self):
        """Reporting line, not department: the two do not always agree."""
        report = self._employee('Rec Report', self.other_dept, 4000.0)
        report.sudo().write({'parent_id': self.sup_employee.id})
        rec = self.env['ksw.pay.recurring'].with_user(self.supervisor).create(
            self._recurring_vals(report))
        self.assertEqual(rec.employee_id, report)

    def test_03_creating_for_somebody_elses_staff_is_refused(self):
        """UserError, not AccessError: the guard fires before the ORM ACL."""
        with self.assertRaises(UserError):
            self.env['ksw.pay.recurring'].with_user(self.supervisor).create(
                self._recurring_vals(self.outsider))

    def test_04_moving_one_onto_somebody_elses_staff_is_refused(self):
        rec = self.env['ksw.pay.recurring'].with_user(self.supervisor).create(
            self._recurring_vals(self.emp))
        with self.assertRaises(UserError):
            rec.write({'employee_id': self.outsider.id})

    def test_05_picker_is_not_wider_than_his_authority(self):
        rec = self.env['ksw.pay.recurring'].with_user(self.supervisor).create(
            self._recurring_vals(self.emp))
        allowed = rec.allowed_employee_ids
        self.assertIn(self.emp, allowed)
        self.assertNotIn(self.outsider, allowed,
                         "the picker must not offer another department's staff")

    def test_06_supervisor_does_not_see_another_departments_entry(self):
        theirs = self.env['ksw.pay.recurring'].sudo().create(
            self._recurring_vals(self.outsider))
        mine = self.env['ksw.pay.recurring'].with_user(self.supervisor).create(
            self._recurring_vals(self.emp))
        visible = self.env['ksw.pay.recurring'].with_user(
            self.supervisor).search([])
        self.assertIn(mine, visible)
        self.assertNotIn(theirs, visible)

    def test_07_supervisor_may_delete_his_own(self):
        rec = self.env['ksw.pay.recurring'].with_user(self.supervisor).create(
            self._recurring_vals(self.emp))
        rec.unlink()
        self.assertFalse(rec.exists())

    def test_08_restricted_component_is_refused(self):
        """The same component gate the batch applies, one level earlier."""
        group = self.env['res.groups'].sudo().create(
            {'name': 'Rec Restricted Group'})
        self.c_mobile.sudo().write({'entry_group_ids': [(6, 0, [group.id])]})
        self.addCleanup(
            self.c_mobile.sudo().write, {'entry_group_ids': [(5, 0, 0)]})
        with self.assertRaises(UserError):
            self.env['ksw.pay.recurring'].with_user(self.supervisor).create(
                self._recurring_vals(self.emp))


class TestRecurringOfficerScope(RecurringAccessCommon):

    def test_01_officer_sees_and_edits_everything(self):
        theirs = self.env['ksw.pay.recurring'].sudo().create(
            self._recurring_vals(self.outsider))
        visible = self.env['ksw.pay.recurring'].with_user(
            self.officer).search([])
        self.assertIn(theirs, visible,
                      "the Officer rule must override the supervisor one")
        theirs.with_user(self.officer).write({'amount': 200.0})
        self.assertAlmostEqual(theirs.amount, 200.0, places=2)

    def test_02_officer_creates_for_anyone(self):
        rec = self.env['ksw.pay.recurring'].with_user(self.officer).create(
            self._recurring_vals(self.outsider))
        self.assertEqual(rec.employee_id, self.outsider)

    def test_03_officer_picker_is_company_wide(self):
        rec = self.env['ksw.pay.recurring'].with_user(self.officer).create(
            self._recurring_vals(self.emp))
        self.assertIn(self.outsider, rec.allowed_employee_ids)


class TestRecurringPullIntoBatch(RecurringAccessCommon):

    def test_01_add_recurring_reaches_a_second_level_subordinate(self):
        """The regression the sudo() in _apply_to_batch prevents.

        A site-scoped batch (driver trips) has no department to filter on, so
        the pull relies entirely on the batch's own scope — which walks the
        reporting chain all the way down. The record rule matches only a
        *direct* parent_id, so without sudo() on the search the standing
        instruction for a team leader's own driver is invisible to the button
        that exists to pull exactly those.
        """
        lead = self._employee('Rec Lead', self.other_dept, 6000.0)
        lead.sudo().write({'parent_id': self.sup_employee.id})
        junior = self._employee('Rec Junior', self.other_dept, 3000.0)
        junior.sudo().write({'parent_id': lead.id})

        # Created by the Officer, so create_uid does not carry it either.
        self.env['ksw.pay.recurring'].with_user(self.officer).create(
            self._recurring_vals(
                junior, component=self.c_trips, quantity=100.0))

        batch = self.env['ksw.pay.batch'].with_user(self.supervisor).create({
            'component_id': self.c_trips.id,
            'site_id': self.site.id,
            'period': self.period,
        })
        batch.with_user(self.supervisor).action_add_recurring()
        batch.invalidate_recordset()
        self.assertIn(junior, batch.entry_ids.employee_id,
                      "a second-level subordinate's recurring entry was skipped")

    def test_02_pull_is_idempotent(self):
        self.env['ksw.pay.recurring'].sudo().create(
            self._recurring_vals(self.emp))
        batch = self.env['ksw.pay.batch'].with_user(self.supervisor).create({
            'component_id': self.c_mobile.id,
            'department_id': self.dept.id,
            'period': self.period,
        })
        batch.with_user(self.supervisor).action_add_recurring()
        batch.invalidate_recordset()
        first = batch.entry_count
        self.assertEqual(first, 1)

        batch.with_user(self.supervisor).action_add_recurring()
        batch.invalidate_recordset()
        self.assertEqual(batch.entry_count, first,
                         "pressing Add Recurring twice must not duplicate")

    def test_03_out_of_scope_recurring_is_not_pulled(self):
        """A recurring entry is company-wide configuration; a batch is not."""
        self.env['ksw.pay.recurring'].sudo().create(
            self._recurring_vals(self.outsider))
        batch = self.env['ksw.pay.batch'].with_user(self.supervisor).create({
            'component_id': self.c_mobile.id,
            'department_id': self.dept.id,
            'period': self.period,
        })
        batch.with_user(self.supervisor).action_add_recurring()
        batch.invalidate_recordset()
        self.assertNotIn(self.outsider, batch.entry_ids.employee_id)
