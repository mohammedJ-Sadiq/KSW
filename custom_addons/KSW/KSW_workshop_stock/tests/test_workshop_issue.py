from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestWorkshopIssue(TransactionCase):
    """Buy tyres into the workshop warehouse, fit them on a repair, complete it:
    the tyres leave stock and are charged to the vehicle's cost centre."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _mkuser(name, login, groups):
            return cls.env['res.users'].create({
                'name': name, 'login': login, 'email': f'{login}@wsstock.test',
                'group_ids': [(6, 0, [cls.env.ref(x).id for x in groups])],
            })
        cls.user_employee = _mkuser('WSS Employee', 'wss_employee', ('base.group_user',))
        cls.employee = cls.env['hr.employee'].create({'name': 'WSS Employee', 'user_id': cls.user_employee.id})
        cls.user_manager = _mkuser('WSS Manager', 'wss_manager', ('base.group_user', 'KSW_workshop.group_workshop_manager'))
        cls.env['hr.employee'].create({'name': 'WSS Manager', 'user_id': cls.user_manager.id})
        cls.user_technician = _mkuser('WSS Technician', 'wss_technician',
                                      ('base.group_user', 'KSW_workshop.group_workshop_technician'))
        cls.env['hr.employee'].create({'name': 'WSS Technician', 'user_id': cls.user_technician.id})

        Account = cls.env['account.account']
        cls.stock_acc = Account.create({'name': 'WSS tyre stock', 'code': '1206999801', 'account_type': 'asset_current'})
        cls.expense_acc = Account.create({'name': 'WSS tyre expense', 'code': '3102999801', 'account_type': 'expense'})
        categ = cls.env['product.category'].create({'name': 'WSS tyres'})
        categ.with_company(cls.env.company).write({
            'property_valuation': 'real_time', 'property_cost_method': 'average',
            'property_stock_valuation_account_id': cls.stock_acc.id,
            'property_account_expense_categ_id': cls.expense_acc.id,
        })
        cls.tyre = cls.env['product.product'].create({
            'name': 'WSS tyre 315/80', 'default_code': '33998', 'is_storable': True,
            'categ_id': categ.id, 'standard_price': 900.0})
        consumption = cls.env.ref('KSW_inventory.location_consumption')
        if not consumption.valuation_account_id:
            consumption.valuation_account_id = cls.expense_acc
        cls.wh = cls.env['stock.warehouse'].create({'name': 'WSS workshop', 'code': 'WSS', 'company_id': cls.env.company.id})
        cls.env['stock.picking.type'].create({
            'name': 'Stock Issue', 'code': 'outgoing', 'sequence_code': 'ISS', 'warehouse_id': cls.wh.id,
            'default_location_src_id': cls.wh.lot_stock_id.id,
            'default_location_dest_id': consumption.id, 'x_ksw_issue': True})
        cls.env['stock.quant']._update_available_quantity(cls.tyre, cls.wh.lot_stock_id, 4.0)

        plan = cls.env['account.analytic.plan'].create({'name': 'WSS cost centres'})
        cls.cost_centre = cls.env['account.analytic.account'].create({'name': 'ايسوزو 777', 'plan_id': plan.id})
        cls.vehicle = cls.env['ksw.fleet.vehicle'].create({
            'name': 'IS777', 'vehicle_type': 'isuzu', 'x_analytic_account_id': cls.cost_centre.id})

    def _repair(self, qty=2.0, vehicle=None):
        vehicle = vehicle or self.vehicle
        request = self.env['ksw.workshop.request'].with_user(self.user_employee).create({
            'vehicle_id': vehicle.id, 'vehicle_type': 'isuzu',
            'driver_id': self.employee.id, 'description': 'Replace two tyres'})
        request.with_user(self.user_manager).action_start()
        request.with_user(self.user_technician).write({
            'x_warehouse_id': self.wh.id,
            'part_line_ids': [(0, 0, {'product_id': self.tyre.id, 'quantity': qty})],
        })
        return request

    def test_completing_the_repair_issues_the_tyres(self):
        request = self._repair()
        line = request.part_line_ids
        self.assertEqual(line.x_available_qty, 4.0)
        self.assertAlmostEqual(line.unit_cost, 900.0)          # estimate until issued
        request.with_user(self.user_manager).action_complete()

        self.assertEqual(request.state, 'completed')
        picking = request.x_issue_picking_id
        self.assertEqual(picking.state, 'done')
        self.assertEqual(picking.origin, request.name)
        self.assertEqual(self.tyre.with_context(location=self.wh.lot_stock_id.id).qty_available, 2.0)
        entry = picking.move_ids.account_move_id
        lines = {l.account_id: l for l in entry.line_ids}
        self.assertAlmostEqual(lines[self.expense_acc].debit, 1800.0)
        self.assertAlmostEqual(lines[self.stock_acc].credit, 1800.0)
        self.assertEqual(lines[self.expense_acc].analytic_distribution, {str(self.cost_centre.id): 100})
        self.assertEqual(line.x_move_id, picking.move_ids)
        self.assertAlmostEqual(line.unit_cost, 900.0)
        self.assertAlmostEqual(request.part_lines_cost, 1800.0)

    def test_short_stock_blocks_completion_and_moves_nothing(self):
        request = self._repair(qty=5.0)
        with self.assertRaises(UserError):
            request.with_user(self.user_manager).action_complete()
        self.assertEqual(request.state, 'in_progress')
        self.assertFalse(request.x_issue_picking_id)
        self.assertEqual(self.tyre.with_context(location=self.wh.lot_stock_id.id).qty_available, 4.0)

    def test_vehicle_without_cost_centre_blocks_completion(self):
        bare = self.env['ksw.fleet.vehicle'].create({'name': 'IS778', 'vehicle_type': 'isuzu'})
        request = self._repair(vehicle=bare)
        with self.assertRaises(UserError):
            request.with_user(self.user_manager).action_complete()
        self.assertEqual(request.state, 'in_progress')

    def test_technician_cannot_complete_and_nothing_is_issued(self):
        request = self._repair()
        with self.assertRaises(UserError):
            request.with_user(self.user_technician).action_complete()
        self.assertFalse(request.x_issue_picking_id)

    def test_issued_line_is_locked(self):
        request = self._repair()
        request.with_user(self.user_manager).action_complete()
        with self.assertRaises(UserError):
            request.part_line_ids.sudo().write({'quantity': 1.0})
        with self.assertRaises(UserError):
            request.part_line_ids.sudo().unlink()

    def test_technician_form_reads(self):
        """What a technician's form actually fetches (Pitfalls #137/#138)."""
        request = self._repair()
        data = request.with_user(self.user_technician).web_read({
            'x_warehouse_id': {'fields': {'display_name': {}}},
            'x_cost_centre_id': {'fields': {'display_name': {}}},
            'part_line_ids': {'fields': {'product_id': {'fields': {'display_name': {}}},
                                         'x_available_qty': {}, 'unit_cost': {}}},
        })
        self.assertEqual(data[0]['part_line_ids'][0]['x_available_qty'], 4.0)

    def test_vehicle_cost_centre_matching(self):
        plan = self.env['account.analytic.plan'].search(
            [('name', '=', 'Cost Centres'), ('parent_id', '=', False)], limit=1)
        if not plan:
            self.skipTest('BAS cost centres not imported in this database')
        isuzu = self.env['account.analytic.plan'].search([('name', '=', 'الايسوزو'), ('parent_id', 'child_of', plan.id)], limit=1)
        acc = self.env['account.analytic.account'].create({'name': 'ايسوزو 9871', 'plan_id': isuzu.id})
        vehicle = self.env['ksw.fleet.vehicle'].create({'name': 'IS9871', 'vehicle_type': 'isuzu'})
        self.env['ksw.fleet.vehicle']._ksw_match_cost_centres()
        self.assertEqual(vehicle.x_analytic_account_id, acc)
