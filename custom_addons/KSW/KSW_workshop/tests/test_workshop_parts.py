"""The pass-through spare-parts list on the repair report.

Parts are issued as they are listed — there is no stock balance to check
against, so what these guard is the *guard*: the parts table is part of the
repair report and must obey the repair report's rule from every entry route,
plus the arithmetic of the cost split.
"""
from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestWorkshopParts(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _mkuser(name, login, group_xmlids=('base.group_user',)):
            return cls.env['res.users'].create({
                'name': name, 'login': login, 'email': f'{login}@wsparts.test',
                'group_ids': [(6, 0, [cls.env.ref(x).id for x in group_xmlids])],
            })

        cls.user_employee = _mkuser('WSP Employee', 'wsparts_employee')
        cls.employee = cls.env['hr.employee'].create({
            'name': 'WSP Employee', 'user_id': cls.user_employee.id,
        })
        cls.user_manager = _mkuser(
            'WSP Manager', 'wsparts_manager',
            group_xmlids=('base.group_user', 'KSW_workshop.group_workshop_manager'),
        )
        cls.env['hr.employee'].create({'name': 'WSP Manager', 'user_id': cls.user_manager.id})
        cls.user_technician = _mkuser(
            'WSP Technician', 'wsparts_technician',
            group_xmlids=('base.group_user', 'KSW_workshop.group_workshop_technician'),
        )
        cls.env['hr.employee'].create({
            'name': 'WSP Technician', 'user_id': cls.user_technician.id,
        })

        cls.vehicle = cls.env['ksw.fleet.vehicle'].create({
            'name': 'WSP-101', 'vehicle_type': 'isuzu',
        })
        cls.part = cls.env['ksw.workshop.part'].create({
            'name': '90915-YZZD4', 'description': 'Oil filter', 'standard_cost': 25.0,
        })

    def _in_progress_request(self):
        request = self.env['ksw.workshop.request'].with_user(self.user_employee).create({
            'vehicle_id': self.vehicle.id, 'description': 'Oil change',
        })
        request.with_user(self.user_manager).action_start()
        return request

    def _add_line(self, request, user, **kwargs):
        vals = {
            'request_id': request.id, 'part_id': self.part.id, 'quantity': 2.0,
        }
        vals.update(kwargs)
        return self.env['ksw.workshop.part.line'].with_user(user).create(vals)

    # ------------------------------------------------------------------
    # The guard, from both entry routes
    # ------------------------------------------------------------------
    def test_technician_can_list_a_part_while_in_progress(self):
        request = self._in_progress_request()
        line = self._add_line(request, self.user_technician)
        self.assertEqual(line.request_id, request)

    def test_requester_cannot_list_a_part_on_their_own_request(self):
        request = self._in_progress_request()
        with self.assertRaises(UserError):
            self._add_line(request, self.user_employee)

    def test_no_parts_once_the_request_leaves_in_progress(self):
        request = self._in_progress_request()
        request.with_user(self.user_manager).action_complete()
        with self.assertRaises(UserError):
            self._add_line(request, self.user_technician)

    def test_both_entry_routes_raise_the_same_message(self):
        """A parent o2m write and a direct line write must not drift apart.

        They share ksw.workshop.request._check_report_edit_rights(); this is
        what proves the sharing is real rather than two copies of the rule.
        """
        request = self._in_progress_request()
        request.with_user(self.user_manager).action_complete()

        with self.assertRaises(UserError) as direct:
            self._add_line(request, self.user_technician)
        with self.assertRaises(UserError) as parent:
            request.with_user(self.user_technician).write({
                'part_line_ids': [(0, 0, {'part_id': self.part.id, 'quantity': 1.0})],
            })
        self.assertEqual(str(direct.exception), str(parent.exception))

    def test_deleting_a_line_obeys_the_same_rule(self):
        request = self._in_progress_request()
        line = self._add_line(request, self.user_technician)
        request.with_user(self.user_manager).action_complete()
        with self.assertRaises(UserError):
            line.with_user(self.user_technician).unlink()

    # ------------------------------------------------------------------
    # Cost
    # ------------------------------------------------------------------
    def test_unit_cost_defaults_from_the_item(self):
        request = self._in_progress_request()
        line = self._add_line(request, self.user_technician)
        self.assertEqual(line.unit_cost, 25.0)
        self.assertEqual(line.subtotal, 50.0)

    def test_unit_cost_is_a_snapshot(self):
        """Changing the catalog cost must not restate a past repair.

        This is why unit_cost is a plain field filled once, not a
        compute/store/readonly=False — a stored compute is still a compute,
        and this test caught it recomputing.
        """
        request = self._in_progress_request()
        line = self._add_line(request, self.user_technician)
        self.part.with_user(self.user_manager).write({'standard_cost': 99.0})
        self.assertEqual(line.unit_cost, 25.0)
        self.assertEqual(line.subtotal, 50.0)

    def test_an_explicit_unit_cost_is_never_overwritten(self):
        request = self._in_progress_request()
        line = self._add_line(request, self.user_technician, unit_cost=7.5)
        self.assertEqual(line.unit_cost, 7.5)

    def test_total_is_listed_parts_plus_other_parts_plus_labor(self):
        request = self._in_progress_request()
        self._add_line(request, self.user_technician)
        request.with_user(self.user_manager).write({
            'parts_cost': 10.0, 'labor_cost': 100.0,
        })
        self.assertEqual(request.part_lines_cost, 50.0)
        self.assertEqual(request.total_cost, 160.0)

    def test_removing_a_line_updates_the_total(self):
        request = self._in_progress_request()
        line = self._add_line(request, self.user_technician)
        line.with_user(self.user_technician).unlink()
        self.assertEqual(request.part_lines_cost, 0.0)

    def test_legacy_parts_cost_still_counts(self):
        """The 878 imported rows carrying a parts cost keep their value."""
        request = self._in_progress_request()
        request.with_user(self.user_manager).write({'parts_cost': 640.0})
        self.assertEqual(request.total_cost, 640.0)

    def test_quantity_must_be_positive(self):
        request = self._in_progress_request()
        with self.assertRaises(ValidationError):
            self._add_line(request, self.user_technician, quantity=0.0)

    # ------------------------------------------------------------------
    # The catalog
    # ------------------------------------------------------------------
    def test_technician_can_quick_create_an_item(self):
        """Inline quick-create from the line is the whole point of the catalog."""
        part = self.env['ksw.workshop.part'].with_user(self.user_technician).create({
            'name': 'WSP-NEW-1',
        })
        self.assertTrue(part.id)

    def test_plain_employee_cannot_create_an_item(self):
        from odoo.exceptions import AccessError
        with self.assertRaises(AccessError):
            self.env['ksw.workshop.part'].with_user(self.user_employee).create({
                'name': 'WSP-NEW-2',
            })

    def test_item_numbers_are_unique(self):
        from psycopg2 import IntegrityError
        with self.assertRaises(IntegrityError), self.cr.savepoint():
            self.env['ksw.workshop.part'].create({'name': '90915-YZZD4'})

    def test_issued_totals_reflect_the_lines(self):
        request = self._in_progress_request()
        self._add_line(request, self.user_technician)
        self.part.invalidate_recordset(['issued_qty', 'issued_value'])
        self.assertEqual(self.part.issued_qty, 2.0)
        self.assertEqual(self.part.issued_value, 50.0)

    def test_display_name_pairs_number_and_description(self):
        self.assertEqual(self.part.display_name, '90915-YZZD4 — Oil filter')

    def test_line_mirrors_the_request_for_reporting(self):
        request = self._in_progress_request()
        line = self._add_line(request, self.user_technician)
        self.assertEqual(line.vehicle_id, self.vehicle)
        self.assertEqual(line.client_id, request.client_id)
        self.assertEqual(line.state, 'in_progress')
        self.assertTrue(line.issue_date)
