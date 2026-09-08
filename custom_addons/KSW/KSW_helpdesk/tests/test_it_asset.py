from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import tagged

from .common import HelpdeskCommon


@tagged('post_install', '-at_install')
class TestItAsset(HelpdeskCommon):
    """Asset register: tagging, custody, maintenance, warranty."""

    def _assign(self, asset, employee, **overrides):
        vals = {'asset_id': asset.id, 'employee_id': employee.id}
        vals.update(overrides)
        self.env['it.asset.assign.wizard'].create(vals).action_confirm()
        return asset

    def _return(self, asset, **overrides):
        vals = {'asset_id': asset.id}
        vals.update(overrides)
        self.env['it.asset.return.wizard'].create(vals).action_confirm()
        return asset

    # ------------------------------------------------------------------
    # Identification
    # ------------------------------------------------------------------
    def test_asset_tag_comes_from_the_sequence(self):
        asset = self._new_asset()
        self.assertTrue(asset.asset_tag.startswith('IT/'), asset.asset_tag)
        self.assertNotEqual(asset.asset_tag, 'New')

    def test_display_name_carries_the_tag(self):
        asset = self._new_asset()
        self.assertEqual(asset.display_name, f'[{asset.asset_tag}] {asset.name}')

    def test_asset_found_by_serial_number(self):
        asset = self._new_asset(serial_number='SN-FIND-ME')
        found = self.env['it.asset'].name_search('SN-FIND-ME')
        self.assertIn(asset.id, [item[0] for item in found])

    def test_new_asset_is_available_and_unassigned(self):
        asset = self._new_asset()
        self.assertEqual(asset.state, 'available')
        self.assertFalse(asset.employee_id)

    # ------------------------------------------------------------------
    # Custody: assign / return
    # ------------------------------------------------------------------
    def test_assigning_puts_the_asset_in_custody(self):
        asset = self._assign(self._new_asset(), self.emp_employee)
        self.assertEqual(asset.state, 'assigned')
        self.assertEqual(asset.employee_id, self.emp_employee)
        self.assertEqual(asset.department_id, self.emp_employee.department_id)

    def test_assigning_opens_one_assignment_row(self):
        asset = self._assign(self._new_asset(), self.emp_employee, condition_out='new')
        self.assertEqual(asset.assignment_count, 1)
        assignment = asset.assignment_ids
        self.assertEqual(assignment.state, 'active')
        self.assertEqual(assignment.condition_out, 'new')
        self.assertFalse(assignment.date_returned)

    def test_an_assigned_asset_cannot_be_assigned_again(self):
        asset = self._assign(self._new_asset(), self.emp_employee)
        with self.assertRaises(UserError):
            self._assign(asset, self.emp_stranger)

    def test_the_assign_button_refuses_an_asset_in_custody(self):
        asset = self._assign(self._new_asset(), self.emp_employee)
        with self.assertRaises(UserError):
            asset.action_open_assign_wizard()

    def test_returning_frees_the_asset_and_closes_the_row(self):
        asset = self._assign(self._new_asset(), self.emp_employee)
        self._return(asset, condition_in='fair')
        self.assertEqual(asset.state, 'available')
        self.assertFalse(asset.employee_id)
        assignment = asset.assignment_ids
        self.assertEqual(assignment.state, 'returned')
        self.assertEqual(assignment.condition_in, 'fair')
        self.assertTrue(assignment.date_returned)

    def test_returning_an_unassigned_asset_is_refused(self):
        asset = self._new_asset()
        with self.assertRaises(UserError):
            self._return(asset)

    def test_custody_history_survives_a_second_assignment(self):
        asset = self._new_asset()
        self._assign(asset, self.emp_employee)
        self._return(asset)
        self._assign(asset, self.emp_stranger)
        self.assertEqual(asset.assignment_count, 2)
        self.assertEqual(asset.employee_id, self.emp_stranger)
        self.assertEqual(
            len(asset.assignment_ids.filtered(lambda a: a.state == 'active')), 1)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------
    def test_sending_to_maintenance_opens_a_repair_row(self):
        asset = self._new_asset()
        asset.action_send_maintenance()
        self.assertEqual(asset.state, 'maintenance')
        self.assertEqual(asset.maintenance_count, 1)
        self.assertEqual(asset.maintenance_ids.state, 'in_progress')

    def test_an_asset_in_custody_cannot_go_to_maintenance(self):
        asset = self._assign(self._new_asset(), self.emp_employee)
        with self.assertRaises(UserError):
            asset.action_send_maintenance()

    def test_returning_from_maintenance_closes_the_repair(self):
        asset = self._new_asset()
        asset.action_send_maintenance()
        asset.action_return_from_maintenance()
        self.assertEqual(asset.state, 'available')
        self.assertEqual(asset.maintenance_ids.state, 'done')
        self.assertTrue(asset.maintenance_ids.date_end)

    def test_returning_an_asset_that_is_not_in_maintenance_is_refused(self):
        asset = self._new_asset()
        with self.assertRaises(UserError):
            asset.action_return_from_maintenance()

    # ------------------------------------------------------------------
    # End of life
    # ------------------------------------------------------------------
    def test_retiring_archives_the_asset(self):
        asset = self._new_asset()
        asset.action_retire()
        self.assertEqual(asset.state, 'retired')
        self.assertFalse(asset.active)

    def test_an_asset_in_custody_cannot_be_retired(self):
        asset = self._assign(self._new_asset(), self.emp_employee)
        with self.assertRaises(UserError):
            asset.action_retire()

    def test_a_lost_asset_can_be_retired(self):
        asset = self._new_asset()
        asset.action_mark_lost()
        self.assertEqual(asset.state, 'lost')
        asset.action_retire()
        self.assertEqual(asset.state, 'retired')

    def test_reactivating_brings_it_back_available(self):
        asset = self._new_asset()
        asset.action_retire()
        asset.action_reactivate()
        self.assertEqual(asset.state, 'available')
        self.assertTrue(asset.active)

    # ------------------------------------------------------------------
    # Warranty
    # ------------------------------------------------------------------
    def _warranty_assets(self):
        today = fields.Date.context_today(self.env['it.asset'])
        return {
            'none': self._new_asset(name='No warranty'),
            'expired': self._new_asset(
                name='Expired', warranty_expiry_date=today - timedelta(days=1)),
            'expiring': self._new_asset(
                name='Expiring', warranty_expiry_date=today + timedelta(days=10)),
            'valid': self._new_asset(
                name='Valid', warranty_expiry_date=today + timedelta(days=365)),
        }

    def test_warranty_status_compute(self):
        for status, asset in self._warranty_assets().items():
            self.assertEqual(asset.warranty_status, status, asset.name)

    def test_warranty_status_search_per_value(self):
        assets = self._warranty_assets()
        scope = self.env['it.asset'].browse([a.id for a in assets.values()])
        for status, asset in assets.items():
            found = self.env['it.asset'].search([
                ('id', 'in', scope.ids), ('warranty_status', '=', status),
            ])
            self.assertEqual(found, asset, status)

    def test_warranty_status_search_negated(self):
        assets = self._warranty_assets()
        scope = self.env['it.asset'].browse([a.id for a in assets.values()])
        found = self.env['it.asset'].search([
            ('id', 'in', scope.ids), ('warranty_status', '!=', 'valid'),
        ])
        self.assertNotIn(assets['valid'], found)
        self.assertIn(assets['expired'], found)

    def test_warranty_status_search_accepts_several_values(self):
        assets = self._warranty_assets()
        scope = self.env['it.asset'].browse([a.id for a in assets.values()])
        found = self.env['it.asset'].search([
            ('id', 'in', scope.ids),
            ('warranty_status', 'in', ['expired', 'expiring']),
        ])
        self.assertEqual(found, assets['expired'] | assets['expiring'])

    def test_warranty_status_search_never_matches_everything(self):
        """An unknown status must select nothing, not the whole register."""
        assets = self._warranty_assets()
        scope = self.env['it.asset'].browse([a.id for a in assets.values()])
        found = self.env['it.asset'].search([
            ('id', 'in', scope.ids), ('warranty_status', '=', 'nonsense'),
        ])
        self.assertFalse(found)

    def test_warranty_status_search_rejects_an_unsupported_operator(self):
        self.assertIs(
            self.env['it.asset']._search_warranty_status('like', 'valid'),
            NotImplemented,
        )

    def test_warranty_cron_schedules_a_todo_for_the_it_team(self):
        """res.groups has no `users` field in Odoo 19 - the cron used to
        raise AttributeError and never remind anybody."""
        today = fields.Date.context_today(self.env['it.asset'])
        due = self._new_asset(
            name='Warranty due', warranty_expiry_date=today + timedelta(days=30))
        far = self._new_asset(
            name='Not yet', warranty_expiry_date=today + timedelta(days=60))

        self.env['it.asset']._cron_warranty_expiry_reminder()

        Activity = self.env['mail.activity']
        due_activities = Activity.search([
            ('res_model', '=', 'it.asset'), ('res_id', '=', due.id),
        ])
        self.assertTrue(due_activities, 'no warranty reminder was scheduled')
        self.assertIn(self.user_agent, due_activities.mapped('user_id'))
        self.assertIn(self.user_agent2, due_activities.mapped('user_id'))
        self.assertNotIn(self.user_employee, due_activities.mapped('user_id'))
        self.assertFalse(Activity.search([
            ('res_model', '=', 'it.asset'), ('res_id', '=', far.id),
        ]))

    def test_warranty_cron_skips_archived_assets(self):
        today = fields.Date.context_today(self.env['it.asset'])
        asset = self._new_asset(
            name='Retired but under warranty',
            warranty_expiry_date=today + timedelta(days=30))
        asset.action_retire()
        self.env['it.asset']._cron_warranty_expiry_reminder()
        self.assertFalse(self.env['mail.activity'].search([
            ('res_model', '=', 'it.asset'), ('res_id', '=', asset.id),
        ]))

    # ------------------------------------------------------------------
    # Link from a ticket
    # ------------------------------------------------------------------
    def test_a_ticket_can_point_at_the_requesters_asset(self):
        asset = self._assign(self._new_asset(), self.emp_employee)
        ticket = self._new_ticket(user=self.user_employee, asset_id=asset.id)
        self.assertEqual(ticket.asset_id, asset)

    def test_category_asset_count(self):
        self.assertEqual(self.asset_category.asset_count, 0)
        self._new_asset()
        self._new_asset(name='Second')
        self.asset_category.invalidate_recordset()
        self.assertEqual(self.asset_category.asset_count, 2)
