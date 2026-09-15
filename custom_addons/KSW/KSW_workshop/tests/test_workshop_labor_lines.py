"""Labor / service lines, and the technician register behind their picker.

Two things are under test here. The register (ksw.workshop.technician) is a
single store with a checkbox on the employee form as its only other face —
so what matters is that the checkbox round-trips, including the second tick
after an untick, which a plain create() would answer with a unique-index
violation. The lines themselves are part of the repair report, so they must
obey the repair report's rule from every entry route, exactly as the spare
parts table does.
"""
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestWorkshopLaborLines(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _mkuser(name, login, group_xmlids=('base.group_user',)):
            return cls.env['res.users'].create({
                'name': name, 'login': login, 'email': f'{login}@wslabor.test',
                'group_ids': [(6, 0, [cls.env.ref(x).id for x in group_xmlids])],
            })

        cls.user_employee = _mkuser('WSL Employee', 'wslabor_employee')
        cls.employee = cls.env['hr.employee'].create({
            'name': 'WSL Employee', 'user_id': cls.user_employee.id,
        })
        cls.user_manager = _mkuser(
            'WSL Manager', 'wslabor_manager',
            group_xmlids=('base.group_user', 'KSW_workshop.group_workshop_manager'),
        )
        cls.env['hr.employee'].create({'name': 'WSL Manager', 'user_id': cls.user_manager.id})
        cls.user_technician = _mkuser(
            'WSL Technician', 'wslabor_technician',
            group_xmlids=('base.group_user', 'KSW_workshop.group_workshop_technician'),
        )
        cls.technician_employee = cls.env['hr.employee'].create({
            'name': 'WSL Technician', 'user_id': cls.user_technician.id,
        })

        cls.vehicle = cls.env['ksw.fleet.vehicle'].create({
            'name': 'WSL-301', 'vehicle_type': 'isuzu',
        })
        cls.external = cls.env.ref('KSW_workshop.technician_external_service_location')

    def _in_progress_request(self):
        request = self.env['ksw.workshop.request'].with_user(self.user_employee).create({
            'vehicle_id': self.vehicle.id,
            'vehicle_type': 'isuzu',
            'driver_id': self.employee.id,
            'description': 'Clutch slipping',
        })
        request.with_user(self.user_manager).action_start()
        return request

    def _add_line(self, request, user, **kwargs):
        vals = {'request_id': request.id, 'description': 'Clutch overhaul'}
        vals.update(kwargs)
        return self.env['ksw.workshop.labor.line'].with_user(user).create(vals)

    # ------------------------------------------------------------------
    # The register
    # ------------------------------------------------------------------
    def test_external_service_location_is_offered(self):
        self.assertTrue(self.external.is_external)
        self.assertFalse(self.external.employee_id)

    def test_external_entry_sorts_last(self):
        """People first, "External Service Location" at the foot of the list."""
        self.technician_employee.x_is_workshop_technician = True
        listed = self.env['ksw.workshop.technician'].search([])
        self.assertEqual(listed[-1], self.external)

    def test_employee_flag_registers_the_technician(self):
        self.assertFalse(self.technician_employee.x_is_workshop_technician)
        self.technician_employee.x_is_workshop_technician = True
        registered = self.env['ksw.workshop.technician'].search([
            ('employee_id', '=', self.technician_employee.id),
        ])
        self.assertEqual(len(registered), 1)
        self.assertEqual(registered.name, 'WSL Technician')
        self.assertTrue(self.technician_employee.x_is_workshop_technician)

    def test_unticking_archives_rather_than_deletes(self):
        """A past labor line must keep naming someone after they stop being
        a technician, so unregistering archives the row."""
        self.technician_employee.x_is_workshop_technician = True
        row = self.env['ksw.workshop.technician'].search([
            ('employee_id', '=', self.technician_employee.id),
        ])
        self.technician_employee.x_is_workshop_technician = False
        self.assertFalse(self.technician_employee.x_is_workshop_technician)
        self.assertFalse(row.exists().active)
        self.assertTrue(row.with_context(active_test=False).exists())

    def test_reticking_revives_instead_of_colliding(self):
        """The second tick must not hit the unique index on employee_id."""
        self.technician_employee.x_is_workshop_technician = True
        self.technician_employee.x_is_workshop_technician = False
        self.technician_employee.x_is_workshop_technician = True
        rows = self.env['ksw.workshop.technician'].with_context(active_test=False).search([
            ('employee_id', '=', self.technician_employee.id),
        ])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows.active)

    def test_name_follows_the_employee(self):
        self.technician_employee.x_is_workshop_technician = True
        self.technician_employee.name = 'WSL Technician Renamed'
        row = self.env['ksw.workshop.technician'].search([
            ('employee_id', '=', self.technician_employee.id),
        ])
        self.assertEqual(row.name, 'WSL Technician Renamed')

    def test_flag_is_searchable(self):
        """Odoo 19 rewrites '=' on a boolean to 'in' before the search method
        sees it, so both spellings have to land on the same records."""
        self.technician_employee.x_is_workshop_technician = True
        Employee = self.env['hr.employee']
        found = Employee.search([('x_is_workshop_technician', '=', True)])
        self.assertIn(self.technician_employee, found)
        self.assertNotIn(self.employee, found)
        self.assertNotIn(
            self.technician_employee,
            Employee.search([('x_is_workshop_technician', '=', False)]),
        )

    # ------------------------------------------------------------------
    # The lines
    # ------------------------------------------------------------------
    def test_technician_can_add_a_line_while_in_progress(self):
        request = self._in_progress_request()
        self.technician_employee.x_is_workshop_technician = True
        registered = self.env['ksw.workshop.technician'].search([
            ('employee_id', '=', self.technician_employee.id),
        ])
        line = self._add_line(
            request, self.user_technician, technician_id=registered.id)
        self.assertEqual(request.labor_line_ids, line)
        self.assertEqual(line.technician_id, registered)

    def test_a_line_can_name_the_external_service_location(self):
        request = self._in_progress_request()
        line = self._add_line(
            request, self.user_manager, technician_id=self.external.id)
        self.assertTrue(line.technician_id.is_external)

    def test_requester_cannot_add_a_line(self):
        request = self._in_progress_request()
        with self.assertRaises(UserError):
            self._add_line(request, self.user_employee)

    def test_no_line_after_the_request_is_completed(self):
        request = self._in_progress_request()
        request.with_user(self.user_manager).action_complete()
        with self.assertRaises(UserError):
            self._add_line(request, self.user_technician)

    def test_line_needs_a_description(self):
        self.assertTrue(self.env['ksw.workshop.labor.line']._fields['description'].required)

    def test_lines_are_numbered_in_table_order(self):
        request = self._in_progress_request()
        first = self._add_line(request, self.user_manager, description='Strip gearbox')
        second = self._add_line(request, self.user_manager, description='Fit clutch kit')
        self.assertEqual((first.line_no, second.line_no), (1, 2))
        # Reordering renumbers: the column follows the table, not creation.
        second.sequence = 1
        self.assertEqual((first.line_no, second.line_no), (2, 1))

    def test_labor_fee_stays_a_single_figure_on_the_request(self):
        """The lines itemise the work, not a price per task — the fee is one
        figure, and it is the one Total Cost adds up."""
        request = self._in_progress_request()
        self._add_line(request, self.user_manager)
        request.with_user(self.user_manager).write({'labor_cost': 400.0})
        self.assertNotIn('labor_cost', self.env['ksw.workshop.labor.line']._fields)
        self.assertEqual(request.total_cost, 400.0)
