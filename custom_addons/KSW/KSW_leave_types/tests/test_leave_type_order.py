from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestLeaveTypeOrder(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        LeaveType = cls.env['hr.leave.type']
        cls.t10 = LeaveType.create({'name': 'Order 10', 'code': '10', 'sequence': 1})
        cls.t2 = LeaveType.create({'name': 'Order 2', 'code': '2', 'sequence': 50})
        cls.t_none = LeaveType.create({'name': 'Order none', 'sequence': 0})
        cls.mine = cls.t10 | cls.t2 | cls.t_none

    def test_list_follows_numeric_code(self):
        """2 before 10 (numeric, not text), uncoded types last."""
        found = self.env['hr.leave.type'].search([('id', 'in', self.mine.ids)])
        self.assertEqual(found.ids, [self.t2.id, self.t10.id, self.t_none.id])

    def test_picker_with_employee_follows_numeric_code(self):
        """Core re-sorts in Python when an employee is in context."""
        employee = self.env['hr.employee'].create({'name': 'Order Emp'})
        found = self.env['hr.leave.type'].with_context(
            employee_id=employee.id).search([('id', 'in', self.mine.ids)])
        self.assertEqual(found.ids, [self.t2.id, self.t10.id, self.t_none.id])

    def test_new_request_starts_without_a_type(self):
        """Core would preselect the lowest-sequence type; KSW leaves it empty."""
        defaults = self.env['hr.leave'].default_get(
            ['holiday_status_id', 'request_unit_hours'])
        self.assertFalse(defaults.get('holiday_status_id'))
        self.assertFalse(defaults.get('request_unit_hours'))

    def test_a_named_type_is_still_prefilled(self):
        """The Extend Vacation button passes its type through context."""
        defaults = self.env['hr.leave'].with_context(
            default_holiday_status_id=self.t2.id).default_get(['holiday_status_id'])
        self.assertEqual(defaults['holiday_status_id'], self.t2.id)
