"""The "this year" history panel on a workshop request.

The workshop's equivalent of the Time Off form's "<employee>'s summary (2026)"
panel: what this vehicle, and this driver under this client, already had done
this year.
"""
from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestWorkshopHistoryPanel(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Request = cls.env['ksw.workshop.request']

        cls.employee = cls.env['hr.employee'].create({'name': 'WSH Requester'})
        cls.driver = cls.env['hr.employee'].create({'name': 'WSH Driver'})
        cls.other_driver = cls.env['hr.employee'].create({'name': 'WSH Other Driver'})

        cls.client = cls.env.company.partner_id
        cls.other_client = cls.env['res.partner'].create({
            'name': 'WSH Other Client', 'customer_rank': 1,
        })
        cls.env['ksw.workshop.client'].create({'partner_id': cls.other_client.id})

        cls.vehicle = cls.env['ksw.fleet.vehicle'].create({
            'name': 'WSH-201', 'vehicle_type': 'isuzu', 'client_id': cls.client.id,
        })
        cls.other_vehicle = cls.env['ksw.fleet.vehicle'].create({
            'name': 'WSH-202', 'vehicle_type': 'isuzu', 'client_id': cls.client.id,
        })

    def _make(self, **kwargs):
        vals = {
            'employee_id': self.employee.id,
            'client_id': self.client.id,
            'vehicle_id': self.vehicle.id,
            'driver_id': self.driver.id,
            'description': 'Service',
        }
        vals.update(kwargs)
        return self.Request.create(vals)

    def _backdate(self, request, when):
        # create_date is a magic column, so the ORM will not write it.
        self.env.cr.execute(
            'UPDATE ksw_workshop_request SET create_date = %s WHERE id = %s',
            (when, request.id),
        )
        request.invalidate_recordset(['create_date'])

    # ------------------------------------------------------------------
    def test_panel_lists_a_past_job_on_the_same_vehicle(self):
        past = self._make()
        current = self._make()
        self.assertIn(past, current.x_vehicle_history_ids)
        self.assertEqual(current.x_vehicle_history_count, 1)

    def test_panel_excludes_the_request_itself(self):
        current = self._make()
        self.assertNotIn(current, current.x_vehicle_history_ids)

    def test_panel_excludes_another_vehicle(self):
        other = self._make(vehicle_id=self.other_vehicle.id)
        current = self._make()
        self.assertNotIn(other, current.x_vehicle_history_ids)

    def test_panel_excludes_rejected_requests(self):
        rejected = self._make()
        rejected.sudo().write({'state': 'rejected'})
        current = self._make()
        self.assertNotIn(rejected, current.x_vehicle_history_ids)

    def test_panel_excludes_last_year(self):
        old = self._make()
        self._backdate(old, fields.Datetime.now() - relativedelta(years=1))
        current = self._make()
        self.assertNotIn(old, current.x_vehicle_history_ids)

    def test_panel_totals_what_was_spent_on_the_vehicle(self):
        past = self._make()
        past.sudo().write({'parts_cost': 100.0, 'labor_cost': 50.0})
        current = self._make()
        self.assertEqual(current.x_vehicle_history_cost, 150.0)

    # ------------------------------------------------------------------
    # The driver half is the one that needs the client stated
    # ------------------------------------------------------------------
    def test_driver_history_lists_other_vehicles_of_the_same_client(self):
        past = self._make(vehicle_id=self.other_vehicle.id)
        current = self._make()
        self.assertIn(past, current.x_driver_history_ids)
        self.assertEqual(current.x_driver_history_count, 1)

    def test_driver_history_is_scoped_to_the_client(self):
        """A driver can move between clients; mixing them would misread as
        one relationship's history."""
        elsewhere = self._make(client_id=self.other_client.id, vehicle_id=False,
                               is_cash_customer=True,
                               x_cash_customer_name='WSH Cash',
                               x_cash_vehicle_number='X-1')
        current = self._make()
        self.assertNotIn(elsewhere, current.x_driver_history_ids)

    def test_driver_history_excludes_another_driver(self):
        other = self._make(driver_id=self.other_driver.id,
                           vehicle_id=self.other_vehicle.id)
        current = self._make()
        self.assertNotIn(other, current.x_driver_history_ids)

    # ------------------------------------------------------------------
    def test_panel_computes_on_an_unsaved_record(self):
        """The one screen the panel exists for is a form that has not been
        saved yet, where `id` is a NewId no domain can express."""
        self._make()
        draft = self.Request.new({
            'employee_id': self.employee.id,
            'client_id': self.client.id,
            'vehicle_id': self.vehicle.id,
            'driver_id': self.driver.id,
            'description': 'Service',
        })
        self.assertEqual(draft.x_vehicle_history_count, 1)

    def test_panel_is_empty_before_a_vehicle_is_picked(self):
        draft = self.Request.new({
            'employee_id': self.employee.id,
            'client_id': self.client.id,
            'description': 'Service',
        })
        self.assertFalse(draft.x_vehicle_history_ids)
        self.assertFalse(draft.x_driver_history_ids)

    def test_panel_fields_carry_no_model_level_groups(self):
        """A model-level groups= would strip them from fields_get() for users
        outside it, and the History page's invisible= expressions reference
        them — the OWL "field is undefined" crash."""
        fields_def = self.Request._fields
        for name in ('x_vehicle_history_ids', 'x_driver_history_ids',
                     'x_vehicle_history_count', 'x_driver_history_count',
                     'x_vehicle_history_cost'):
            self.assertFalse(
                fields_def[name].groups,
                f'{name} carries a model-level groups= — use view-level groups instead',
            )
