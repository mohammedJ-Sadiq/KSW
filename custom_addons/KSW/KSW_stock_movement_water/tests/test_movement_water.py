from odoo.addons.KSW_water_delivery.tests.test_water_delivery import WaterDeliveryCommon
from odoo.tests import tagged


@tagged('post_install', '-at_install')
class TestMovementWater(WaterDeliveryCommon):

    def _rows(self, note):
        wizard = self.env['ksw.stock.movement.wizard'].create({
            'location_ids': [(6, 0, note.location_id.ids)],
            'product_ids': [(6, 0, note.move_ids.product_id.ids)],
        })
        wizard.action_show()
        return wizard.line_ids.filtered(lambda r: r.picking_id == note)

    def test_the_driver_and_the_price_of_a_water_note(self):
        note = self._issue_note(entered_qty=3.0).picking_id.sudo()
        row = self._rows(note)
        self.assertEqual(row.x_driver, self.driver.name)
        self.assertFalse(row.x_dispatcher, 'issued by the driver himself')
        self.assertEqual(row.price_unit, 200.0)
        self.assertEqual(row.amount_untaxed, 600.0)
        self.assertEqual(row.amount_total, 690.0)

    def test_the_dispatcher_of_a_dispatched_note(self):
        note = self._issue_note().picking_id.sudo()
        desk = self.env['res.users'].create({'name': 'Desk Person', 'login': 'desk.sm@ksw.test'})
        dispatch = self.env['ksw.water.dispatch'].sudo().create({
            'picking_type_id': self.picking_type.id, 'partner_id': self.customer.id,
            'product_id': self.water.id, 'dispatcher_id': desk.id})
        note.x_dispatch_id = dispatch
        self.assertEqual(self._rows(note).x_dispatcher, 'Desk Person')

    def _cost_centres(self):
        Plan, Account = self.env['account.analytic.plan'], self.env['account.analytic.account']
        made = []
        for name in ('SM Vehicle', 'SM Branch', 'SM Employee'):
            made.append(Account.create({'name': name, 'plan_id': Plan.create({'name': name}).id}))
        vehicle_cc, branch_cc, employee_cc = made
        self.vehicle.x_analytic_account_id = vehicle_cc
        self.picking_type.x_analytic_account_id = branch_cc
        self.driver.sudo().x_analytic_account_id = employee_cc
        return vehicle_cc, branch_cc, employee_cc

    def test_an_uninvoiced_delivery_shows_its_cost_centres(self):
        vehicle_cc, branch_cc, employee_cc = self._cost_centres()
        note = self._issue_note().picking_id.sudo()
        row = self._rows(note)
        self.assertEqual(row.invoice_status, 'to invoice', 'not invoiced yet')
        self.assertEqual((row.x_cc_branch_id, row.x_cc_vehicle_id, row.x_cc_employee_id),
                         (branch_cc, vehicle_cc, employee_cc))

    def test_a_later_change_does_not_move_an_old_delivery(self):
        vehicle_cc, _branch_cc, _employee_cc = self._cost_centres()
        note = self._issue_note().picking_id.sudo()
        self.vehicle.x_analytic_account_id = self.env['account.analytic.account'].create({
            'name': 'Another', 'plan_id': vehicle_cc.plan_id.id})
        self.assertEqual(self._rows(note).x_cc_vehicle_id, vehicle_cc)

    def test_the_row_opens_the_note_and_its_invoice_in_a_pop_up(self):
        note = self._issue_note().picking_id.sudo()
        invoice = note._water_create_invoice()
        row = self._rows(note)
        action = row.action_open_picking()
        self.assertEqual(action['tag'], 'ksw_open_record_dialog')
        self.assertEqual((action['params']['res_model'], action['params']['res_id']),
                         ('stock.picking', note.id))
        self.assertEqual(action['params']['form_view_ref'],
                         'KSW_water_delivery.view_water_delivery_note_form')
        self.assertEqual(row.invoice_move_id, invoice.id)
        action = row.action_open_invoice()
        self.assertEqual((action['params']['res_model'], action['params']['res_id']),
                         ('account.move', invoice.id))

    def test_the_note_shows_its_dispatcher(self):
        note = self._issue_note().picking_id.sudo()
        desk = self.env['res.users'].create({'name': 'Desk Two', 'login': 'desk2.sm@ksw.test'})
        note.x_dispatch_id = self.env['ksw.water.dispatch'].sudo().create({
            'picking_type_id': self.picking_type.id, 'partner_id': self.customer.id,
            'product_id': self.water.id, 'dispatcher_id': desk.id})
        self.assertEqual(note.x_dispatcher_id, desk)
