"""Tests: the Manager Assistant delegation plumbing.

The scope behaviour lives with the models it guards (KSW_annual_leave,
KSW_deduction). What is pinned here is the delegation link itself and the
group's implication set -- the cheapest possible guard against the one failure
mode that would silently dissolve the whole feature.

Everything the delegation grants is ADDITIVE: record rules OR together across
a user's groups, so a state-gated rule can only ever widen scope, never
restrict it. If `group_manager_assistant` ever implied a blanket group
(hr_holidays.group_hr_holidays_user, KSW_annual_leave.group_leave_officer,
KSW_deduction.group_deduction_officer) the "before any approval" edit gate
would evaporate -- and the last of those makes
`ksw.deduction.x_can_dm_approve` unconditionally True, handing over the exact
step this feature exists to protect.
"""
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class TestManagerAssistant(TransactionCase):

    ASSISTANT_GROUP = 'KSW_base_security.group_manager_assistant'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _mkuser(name, login, groups=()):
            return cls.env['res.users'].create({
                'name': name,
                'login': login,
                'email': f'{login}@asstbase.test',
                'group_ids': [(6, 0, [cls.env.ref('base.group_user').id]
                               + [cls.env.ref(g).id for g in groups])],
            })

        cls.user_mgr = _mkuser('Base Deleg Manager', 'asstbase_mgr')
        cls.user_asst = _mkuser('Base Deleg Assistant', 'asstbase_asst',
                                (cls.ASSISTANT_GROUP,))
        cls.user_plain = _mkuser('Base Deleg Plain', 'asstbase_plain')
        # No employee records here on purpose: this class covers the EXPLICIT
        # half. The automatic (own-manager) half is covered by
        # test_auto_derives_own_manager below and end-to-end in
        # KSW_annual_leave.

    def test_auto_derives_own_manager(self):
        """Holding the right is enough when you assist your own manager --
        the common case, and the one that used to look like a bug because a
        missing nomination gives no feedback at all."""
        mgr_emp = self.env['hr.employee'].create({
            'name': 'Auto Mgr Emp', 'user_id': self.user_mgr.id})
        asst_emp = self.env['hr.employee'].create({
            'name': 'Auto Asst Emp', 'user_id': self.user_asst.id,
            'parent_id': mgr_emp.id, 'leave_manager_id': self.user_mgr.id})
        self.user_asst.invalidate_recordset()
        self.assertEqual(
            self.user_asst._ksw_assisted_manager_ids(), [self.user_mgr.id],
            'own manager must be picked up with no nomination at all')
        # ...and it is still gated on the access right.
        asst_emp.write({'parent_id': mgr_emp.id})
        self.assertEqual(self.user_plain._ksw_assisted_manager_ids(), [])

    def test_never_assists_self(self):
        """A self-managed employee must not gain their own team this way."""
        emp = self.env['hr.employee'].create({
            'name': 'Self Managed', 'user_id': self.user_asst.id})
        emp.write({'parent_id': emp.id, 'leave_manager_id': self.user_asst.id})
        self.user_asst.invalidate_recordset()
        self.assertNotIn(self.user_asst.id,
                         self.user_asst._ksw_assisted_manager_ids())

    def test_inverse_field_mirrors_the_nomination(self):
        self.user_mgr.sudo().write({'x_assistant_ids': [(4, self.user_asst.id)]})
        self.assertIn(self.user_mgr, self.user_asst.sudo().x_assisted_manager_ids)

    def test_helper_needs_both_keys(self):
        # Key two only (group, no nomination).
        self.assertEqual(self.user_asst._ksw_assisted_manager_ids(), [])
        # Key one only (nomination, no group).
        self.user_mgr.sudo().write({'x_assistant_ids': [(4, self.user_plain.id)]})
        self.assertEqual(self.user_plain._ksw_assisted_manager_ids(), [])
        # Both.
        self.user_mgr.sudo().write({'x_assistant_ids': [(4, self.user_asst.id)]})
        self.assertEqual(
            self.user_asst._ksw_assisted_manager_ids(), [self.user_mgr.id])

    def test_link_is_writable_from_the_assistant_side(self):
        """The admin granting the right is looking at the ASSISTANT's form;
        forcing them to the manager's record is how the right ends up
        granted with an empty (inert) delegation."""
        self.user_asst.sudo().write(
            {'x_assisted_manager_ids': [(4, self.user_mgr.id)]})
        self.assertIn(self.user_asst, self.user_mgr.sudo().x_assistant_ids)
        self.assertEqual(
            self.user_asst._ksw_assisted_manager_ids(), [self.user_mgr.id])

    def test_self_delegation_rejected(self):
        with self.assertRaises(ValidationError):
            self.user_mgr.sudo().write(
                {'x_assistant_ids': [(4, self.user_mgr.id)]})

    def test_self_delegation_rejected_from_inverse_side(self):
        """A constrains only fires for the field actually in vals, so the
        inverse needs to be listed on the decorator too."""
        with self.assertRaises(ValidationError):
            self.user_mgr.sudo().write(
                {'x_assisted_manager_ids': [(4, self.user_mgr.id)]})

    def test_warning_flag_tracks_the_access_right(self):
        self.assertTrue(self.user_asst.x_has_manager_assistant_right)
        self.assertFalse(self.user_plain.x_has_manager_assistant_right)

    def test_group_implies_only_what_it_must(self):
        group = self.env.ref(self.ASSISTANT_GROUP)
        implied = group.all_implied_ids

        # Required: without Time Off Responsible the manager leave form hides
        # the employee_id field entirely and the assistant cannot file at all.
        self.assertIn(
            self.env.ref('hr_holidays.group_hr_holidays_responsible'), implied)

        # Forbidden: each of these alone collapses the design.
        for xmlid in (
            'hr_holidays.group_hr_holidays_user',
            'hr_holidays.group_hr_holidays_manager',
            'KSW_annual_leave.group_leave_officer',
            'KSW_deduction.group_deduction_officer',
            'KSW_annual_leave.group_annual_leave_hr',
            'KSW_annual_leave.group_annual_leave_gm',
            'KSW_annual_leave.group_annual_leave_acc',
        ):
            group_rec = self.env.ref(xmlid, raise_if_not_found=False)
            if group_rec:
                self.assertNotIn(
                    group_rec, implied,
                    f'group_manager_assistant must never imply {xmlid}')
