from odoo.exceptions import AccessError
from odoo.tests.common import tagged

from .common import HelpdeskCommon


@tagged('post_install', '-at_install')
class TestHelpdeskSecurity(HelpdeskCommon):
    """Record rules and ACLs, exercised as the real users."""

    # ------------------------------------------------------------------
    # Every internal user is a Helpdesk user (the <function> implication)
    # ------------------------------------------------------------------
    def test_every_internal_user_can_submit_a_ticket(self):
        self.assertTrue(self.user_stranger.has_group('KSW_helpdesk.group_helpdesk_user'))
        self.assertFalse(self.user_stranger.has_group('KSW_helpdesk.group_helpdesk_agent'))

    def test_it_team_implies_helpdesk_user(self):
        self.assertTrue(self.user_agent.has_group('KSW_helpdesk.group_helpdesk_user'))

    # ------------------------------------------------------------------
    # helpdesk.ticket scope
    # ------------------------------------------------------------------
    def test_employee_sees_only_their_own_ticket(self):
        mine = self._new_ticket(user=self.user_employee)
        theirs = self._new_ticket(user=self.user_agent, employee_id=self.emp_stranger.id)
        visible = self.env['helpdesk.ticket'].with_user(self.user_employee).search([])
        self.assertIn(mine, visible)
        self.assertNotIn(theirs, visible)

    def test_manager_sees_a_direct_reports_ticket(self):
        ticket = self._new_ticket(user=self.user_employee)
        visible = self.env['helpdesk.ticket'].with_user(self.user_manager).search([])
        self.assertIn(ticket, visible)

    def test_stranger_cannot_read_someone_elses_ticket(self):
        ticket = self._new_ticket(user=self.user_employee)
        with self.assertRaises(AccessError):
            ticket.with_user(self.user_stranger).read(['name'])

    def test_agent_sees_every_ticket(self):
        mine = self._new_ticket(user=self.user_employee)
        theirs = self._new_ticket(user=self.user_agent, employee_id=self.emp_stranger.id)
        visible = self.env['helpdesk.ticket'].with_user(self.user_agent).search([])
        self.assertIn(mine, visible)
        self.assertIn(theirs, visible)

    def test_employee_cannot_delete_a_ticket(self):
        ticket = self._new_ticket(user=self.user_employee)
        with self.assertRaises(AccessError):
            ticket.with_user(self.user_employee).unlink()

    def test_agent_can_delete_a_ticket(self):
        ticket = self._new_ticket(user=self.user_employee)
        ticket.with_user(self.user_agent).unlink()
        self.assertFalse(ticket.exists())

    # ------------------------------------------------------------------
    # it.asset scope - read-only, and only what is in your custody
    # ------------------------------------------------------------------
    def test_employee_reads_the_asset_in_their_custody(self):
        asset = self._new_asset()
        self._assign(asset, self.emp_employee)
        asset.with_user(self.user_employee).read(['name'])

    def test_manager_reads_a_direct_reports_asset(self):
        asset = self._new_asset()
        self._assign(asset, self.emp_employee)
        asset.with_user(self.user_manager).read(['name'])

    def test_employee_cannot_read_an_unassigned_asset(self):
        asset = self._new_asset()
        with self.assertRaises(AccessError):
            asset.with_user(self.user_employee).read(['name'])

    def test_employee_cannot_read_someone_elses_asset(self):
        asset = self._new_asset()
        self._assign(asset, self.emp_stranger)
        with self.assertRaises(AccessError):
            asset.with_user(self.user_employee).read(['name'])

    def test_employee_cannot_write_their_own_asset(self):
        asset = self._new_asset()
        self._assign(asset, self.emp_employee)
        with self.assertRaises(AccessError):
            asset.with_user(self.user_employee).write({'notes': 'mine now'})

    def test_employee_cannot_create_an_asset(self):
        with self.assertRaises(AccessError):
            self.env['it.asset'].with_user(self.user_employee).create({
                'name': 'Smuggled laptop',
                'category_id': self.asset_category.id,
            })

    def test_employee_cannot_browse_the_assignment_history(self):
        asset = self._new_asset()
        self._assign(asset, self.emp_employee)
        with self.assertRaises(AccessError):
            self.env['it.asset.assignment'].with_user(self.user_employee).search([])

    def test_agent_manages_the_whole_register(self):
        asset = self._new_asset()
        asset.with_user(self.user_agent).write({'notes': 'in the store'})
        self.assertEqual(asset.notes, 'in the store')

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------
    def _assign(self, asset, employee):
        wizard = self.env['it.asset.assign.wizard'].create({
            'asset_id': asset.id,
            'employee_id': employee.id,
        })
        wizard.action_confirm()
        return asset
