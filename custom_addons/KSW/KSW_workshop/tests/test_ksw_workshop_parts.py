from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase


class TestKswWorkshopParts(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _mkuser(name, login, group_xmlids=('base.group_user',)):
            return cls.env['res.users'].create({
                'name': name,
                'login': login,
                'email': f'{login}@wsparts.test',
                'group_ids': [(6, 0, [cls.env.ref(xmlid).id for xmlid in group_xmlids])],
            })

        cls.user_employee = _mkuser('WSP Employee', 'wsparts_employee')
        cls.employee = cls.env['hr.employee'].create({
            'name': 'WSP Employee', 'user_id': cls.user_employee.id,
        })

        cls.user_manager = _mkuser(
            'WSP Manager', 'wsparts_manager',
            group_xmlids=('base.group_user', 'KSW_workshop.group_workshop_manager'),
        )
        cls.manager_employee = cls.env['hr.employee'].create({
            'name': 'WSP Manager', 'user_id': cls.user_manager.id,
        })

        cls.user_technician = _mkuser(
            'WSP Technician', 'wsparts_technician',
            group_xmlids=('base.group_user', 'KSW_workshop.group_workshop_technician'),
        )
        cls.technician_employee = cls.env['hr.employee'].create({
            'name': 'WSP Technician', 'user_id': cls.user_technician.id,
        })

        cls.user_other = _mkuser('WSP Other Employee', 'wsparts_other')
        cls.other_employee = cls.env['hr.employee'].create({
            'name': 'WSP Other Employee', 'user_id': cls.user_other.id,
        })

        cls.vehicle = cls.env['ksw.fleet.vehicle'].create({'name': 'WSP-1', 'vehicle_type': 'isuzu'})

    def _make_part(self, **kwargs):
        vals = {'name': 'Oil Filter'}
        vals.update(kwargs)
        return self.env['ksw.workshop.part'].with_user(self.user_manager).create(vals)

    def _stock(self, part, qty, cost=10.0, user=None):
        return self.env['ksw.workshop.part.move'].with_user(user or self.user_manager).create({
            'part_id': part.id, 'move_type': 'in', 'quantity': qty, 'unit_cost': cost,
        })

    def _make_request(self, **kwargs):
        vals = {'vehicle_id': self.vehicle.id, 'description': 'Repair'}
        vals.update(kwargs)
        return self.env['ksw.workshop.request'].with_user(self.user_employee).create(vals)

    def _started_request(self):
        request = self._make_request()
        request.with_user(self.user_manager).action_start()
        return request

    def _add_line(self, request, part, qty, user=None):
        return self.env['ksw.workshop.part.line'].with_user(user or self.user_technician).create({
            'request_id': request.id, 'part_id': part.id, 'quantity': qty,
        })

    # ------------------------------------------------------------------
    # Catalog & lifecycle
    # ------------------------------------------------------------------
    def test_technician_created_part_lands_in_draft(self):
        part = self.env['ksw.workshop.part'].with_user(self.user_technician).create({'name': 'Belt'})
        self.assertEqual(part.state, 'draft')

    def test_manager_created_part_is_confirmed(self):
        part = self._make_part()
        self.assertEqual(part.state, 'confirmed')

    def test_technician_cannot_confirm_part(self):
        part = self.env['ksw.workshop.part'].with_user(self.user_technician).create({'name': 'Belt'})
        with self.assertRaises(UserError):
            part.with_user(self.user_technician).action_confirm()

    def test_manager_can_confirm_draft_part(self):
        part = self.env['ksw.workshop.part'].with_user(self.user_technician).create({'name': 'Belt'})
        part.with_user(self.user_manager).action_confirm()
        self.assertEqual(part.state, 'confirmed')

    def test_part_code_is_unique(self):
        self._make_part(code='OF-100')
        with self.assertRaises(Exception):
            self._make_part(code='OF-100')

    def test_part_with_movements_cannot_be_deleted(self):
        part = self._make_part()
        self._stock(part, 5)
        with self.assertRaises(UserError):
            part.with_user(self.user_manager).unlink()

    def test_part_without_movements_can_be_deleted(self):
        part = self._make_part()
        part.with_user(self.user_manager).unlink()

    def test_display_name_includes_code(self):
        part = self._make_part(code='OF-200', name='Air Filter')
        self.assertIn('OF-200', part.display_name)
        self.assertIn('Air Filter', part.display_name)

    def test_display_name_without_code_is_bare_name(self):
        part = self._make_part(name='Air Filter')
        self.assertEqual(part.display_name, 'Air Filter')

    # ------------------------------------------------------------------
    # Income permission
    # ------------------------------------------------------------------
    def test_manager_can_record_income(self):
        part = self._make_part()
        self._stock(part, 10, cost=5.0)
        self.assertEqual(part.qty_on_hand, 10)

    def test_technician_cannot_record_income(self):
        part = self._make_part()
        with self.assertRaises(UserError):
            self._stock(part, 10, user=self.user_technician)

    def test_employee_cannot_record_income(self):
        part = self._make_part()
        with self.assertRaises(UserError):
            self._stock(part, 10, user=self.user_employee)

    def test_income_updates_part_standard_cost(self):
        part = self._make_part()
        self._stock(part, 10, cost=7.5)
        self.assertEqual(part.standard_cost, 7.5)

    def test_income_quantity_must_be_positive(self):
        part = self._make_part()
        with self.assertRaises(Exception):
            self.env['ksw.workshop.part.move'].with_user(self.user_manager).create({
                'part_id': part.id, 'move_type': 'in', 'quantity': -1, 'unit_cost': 5.0,
            })

    def test_manager_can_edit_income_move_note(self):
        part = self._make_part()
        move = self._stock(part, 10)
        move.with_user(self.user_manager).write({'note': 'delivery #1'})
        self.assertEqual(move.note, 'delivery #1')

    # ------------------------------------------------------------------
    # Ledger correctness
    # ------------------------------------------------------------------
    def test_consuming_a_line_creates_one_out_move_and_decrements_stock(self):
        part = self._make_part()
        self._stock(part, 10)
        request = self._started_request()
        line = self._add_line(request, part, 3)
        self.assertTrue(line.move_id)
        self.assertEqual(line.move_id.move_type, 'out')
        self.assertEqual(part.qty_on_hand, 7)

    def test_changing_line_quantity_updates_the_same_move(self):
        part = self._make_part()
        self._stock(part, 10)
        request = self._started_request()
        line = self._add_line(request, part, 3)
        move = line.move_id
        line.with_user(self.user_technician).write({'quantity': 5})
        self.assertEqual(move.quantity, 5)
        self.assertEqual(len(part.move_ids), 2)  # 1 income + 1 consumption, never a second consumption row
        self.assertEqual(part.qty_on_hand, 5)

    def test_deleting_line_removes_move_and_restores_stock(self):
        part = self._make_part()
        self._stock(part, 10)
        request = self._started_request()
        line = self._add_line(request, part, 4)
        move = line.move_id
        line.with_user(self.user_technician).unlink()
        self.assertEqual(part.qty_on_hand, 10)
        self.assertFalse(move.exists())

    def test_line_unit_cost_defaults_from_part_standard_cost(self):
        part = self._make_part()
        self._stock(part, 10, cost=12.5)
        request = self._started_request()
        line = self._add_line(request, part, 1)
        self.assertEqual(line.unit_cost, 12.5)

    def test_line_unit_cost_snapshot_survives_later_part_cost_change(self):
        part = self._make_part()
        self._stock(part, 10, cost=12.5)
        request = self._started_request()
        line = self._add_line(request, part, 1)
        self._stock(part, 5, cost=20.0)
        self.assertEqual(line.unit_cost, 12.5)

    def test_deleting_request_removes_its_lines_and_movements(self):
        part = self._make_part()
        self._stock(part, 10)
        request = self._started_request()
        self._add_line(request, part, 3)
        request.with_user(self.user_manager).unlink()
        self.assertEqual(part.qty_on_hand, 10)

    # ------------------------------------------------------------------
    # Negative stock
    # ------------------------------------------------------------------
    def test_consumption_beyond_on_hand_is_rejected(self):
        part = self._make_part()
        self._stock(part, 5)
        request = self._started_request()
        with self.assertRaises(ValidationError):
            self._add_line(request, part, 6)

    def test_consumption_exactly_to_zero_is_allowed(self):
        part = self._make_part()
        self._stock(part, 5)
        request = self._started_request()
        line = self._add_line(request, part, 5)
        self.assertEqual(part.qty_on_hand, 0)
        self.assertTrue(line.exists())

    def test_increasing_an_existing_line_beyond_stock_is_rejected_and_rolls_back(self):
        part = self._make_part()
        self._stock(part, 5)
        request = self._started_request()
        line = self._add_line(request, part, 3)
        with self.assertRaises(ValidationError):
            line.with_user(self.user_technician).write({'quantity': 10})
        self.assertEqual(line.quantity, 3)
        self.assertEqual(part.qty_on_hand, 2)

    # ------------------------------------------------------------------
    # State-guard interaction (must mirror, not duplicate)
    # ------------------------------------------------------------------
    def test_technician_can_add_line_while_in_progress(self):
        part = self._make_part()
        self._stock(part, 10)
        request = self._started_request()
        line = self._add_line(request, part, 1)
        self.assertTrue(line.exists())

    def test_technician_cannot_add_line_while_new(self):
        part = self._make_part()
        self._stock(part, 10)
        request = self._make_request()
        with self.assertRaises(UserError):
            self._add_line(request, part, 1)

    def test_technician_cannot_add_line_once_completed(self):
        part = self._make_part()
        self._stock(part, 10)
        request = self._started_request()
        request.with_user(self.user_manager).action_complete()
        with self.assertRaises(UserError):
            self._add_line(request, part, 1)

    def test_employee_cannot_add_line_to_own_request(self):
        part = self._make_part()
        self._stock(part, 10)
        request = self._started_request()
        with self.assertRaises(UserError):
            self._add_line(request, part, 1, user=self.user_employee)

    def test_cannot_change_line_quantity_once_completed(self):
        part = self._make_part()
        self._stock(part, 10)
        request = self._started_request()
        line = self._add_line(request, part, 1)
        request.with_user(self.user_manager).action_complete()
        with self.assertRaises(UserError):
            line.with_user(self.user_technician).write({'quantity': 2})

    def test_cannot_delete_line_once_completed(self):
        part = self._make_part()
        self._stock(part, 10)
        request = self._started_request()
        line = self._add_line(request, part, 1)
        request.with_user(self.user_manager).action_complete()
        with self.assertRaises(UserError):
            line.with_user(self.user_technician).unlink()

    def test_line_guard_message_matches_report_field_guard(self):
        part = self._make_part()
        self._stock(part, 10)
        request = self._started_request()
        request.with_user(self.user_manager).action_complete()

        with self.assertRaises(UserError) as cm_field:
            request.with_user(self.user_technician).write({'odometer_reading': 12345})

        with self.assertRaises(UserError) as cm_line:
            self._add_line(request, part, 1)

        self.assertEqual(str(cm_field.exception), str(cm_line.exception))

    # ------------------------------------------------------------------
    # Cost auto-fill
    # ------------------------------------------------------------------
    def test_parts_cost_sums_part_lines(self):
        part = self._make_part()
        self._stock(part, 10, cost=4.0)
        request = self._started_request()
        self._add_line(request, part, 3)
        self.assertEqual(request.part_lines_cost, 12.0)
        self.assertEqual(request.parts_cost, 12.0)

    def test_parts_cost_adds_manual_extra_cost(self):
        part = self._make_part()
        self._stock(part, 10, cost=4.0)
        request = self._started_request()
        self._add_line(request, part, 3)
        request.with_user(self.user_manager).write({'parts_extra_cost': 8.0})
        self.assertEqual(request.parts_cost, 20.0)

    def test_parts_cost_drops_when_line_deleted(self):
        part = self._make_part()
        self._stock(part, 10, cost=4.0)
        request = self._started_request()
        line = self._add_line(request, part, 3)
        self.assertEqual(request.parts_cost, 12.0)
        line.with_user(self.user_technician).unlink()
        self.assertEqual(request.parts_cost, 0.0)

    def test_manual_extra_cost_survives_adding_a_part_line(self):
        part = self._make_part()
        self._stock(part, 10, cost=4.0)
        request = self._started_request()
        request.with_user(self.user_manager).write({'parts_extra_cost': 8.0})
        self._add_line(request, part, 3)
        self.assertEqual(request.parts_extra_cost, 8.0)
        self.assertEqual(request.parts_cost, 20.0)

    def test_free_text_repairs_parts_is_untouched_by_part_lines(self):
        part = self._make_part()
        self._stock(part, 10, cost=4.0)
        request = self._started_request()
        request.with_user(self.user_technician).write({'repairs_parts': 'Generic bolt, bought locally'})
        self._add_line(request, part, 3)
        self.assertEqual(request.repairs_parts, 'Generic bolt, bought locally')

    # ------------------------------------------------------------------
    # Return after completion
    # ------------------------------------------------------------------
    def test_manager_can_return_line_after_completion(self):
        part = self._make_part()
        self._stock(part, 10, cost=4.0)
        request = self._started_request()
        line = self._add_line(request, part, 3)
        request.with_user(self.user_manager).action_complete()
        self.assertEqual(part.qty_on_hand, 7)

        line.with_user(self.user_manager).action_return_to_stock()
        self.assertEqual(part.qty_on_hand, 10)
        self.assertEqual(line.returned_qty, 3)
        self.assertEqual(request.parts_cost, 0.0)
        # append-only: both the original consumption and the return survive
        self.assertEqual(len(part.move_ids), 3)

    def test_technician_cannot_return_line(self):
        part = self._make_part()
        self._stock(part, 10, cost=4.0)
        request = self._started_request()
        line = self._add_line(request, part, 3)
        request.with_user(self.user_manager).action_complete()
        with self.assertRaises(UserError):
            line.with_user(self.user_technician).action_return_to_stock()

    def test_cannot_return_the_same_line_twice(self):
        part = self._make_part()
        self._stock(part, 10, cost=4.0)
        request = self._started_request()
        line = self._add_line(request, part, 3)
        request.with_user(self.user_manager).action_complete()
        line.with_user(self.user_manager).action_return_to_stock()
        with self.assertRaises(UserError):
            line.with_user(self.user_manager).action_return_to_stock()

    # ------------------------------------------------------------------
    # Record rules
    # ------------------------------------------------------------------
    def test_employee_cannot_search_other_requests_part_lines(self):
        part = self._make_part()
        self._stock(part, 10)
        request = self._started_request()  # created by self.user_employee
        line = self._add_line(request, part, 1)
        found = self.env['ksw.workshop.part.line'].with_user(self.user_other).search(
            [('id', '=', line.id)]
        )
        self.assertFalse(found)

    def test_employee_can_search_own_requests_part_lines(self):
        part = self._make_part()
        self._stock(part, 10)
        request = self._started_request()  # created by self.user_employee
        line = self._add_line(request, part, 1)
        found = self.env['ksw.workshop.part.line'].with_user(self.user_employee).search(
            [('id', '=', line.id)]
        )
        self.assertEqual(found, line)

    def test_technician_sees_in_progress_part_lines_via_search(self):
        part = self._make_part()
        self._stock(part, 10)
        request = self._started_request()
        line = self._add_line(request, part, 1)
        found = self.env['ksw.workshop.part.line'].with_user(self.user_technician).search(
            [('id', '=', line.id)]
        )
        self.assertEqual(found, line)
