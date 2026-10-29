"""Credit limit on water deliveries: stop before, hold after (Oct 2026)."""
from odoo.exceptions import UserError
from odoo.tests import tagged

from .test_offline_capture import OfflineCaptureCommon


@tagged('post_install', '-at_install')
class TestWaterCredit(OfflineCaptureCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if 'risk_exception' not in cls.env['res.partner']._fields:
            return
        # 200 a unit + VAT: one unit (230) is over a 100 limit; sales orders
        # not invoiced yet count towards it.
        cls.customer.write({'credit_limit': 100.0, 'risk_sale_order_include': True})
        cls.risk_manager = cls.env['res.users'].create({
            'name': 'Risk Boss', 'login': 'riskboss@ksw.test',
            'group_ids': [(6, 0, [
                cls.env.ref('KSW_water_delivery.group_water_driver').id,
                cls.env.ref('account_financial_risk.group_account_financial_risk_manager').id,
            ])],
        })
        cls.env['hr.employee'].create({'name': 'Risk Boss', 'user_id': cls.risk_manager.id})

    def setUp(self):
        super().setUp()
        if 'risk_exception' not in self.env['res.partner']._fields:
            self.skipTest('account_financial_risk is not installed')

    def test_the_form_refuses_a_customer_over_his_limit(self):
        with self.assertRaisesRegex(UserError, 'over the credit limit'):
            self._issue_note()

    def test_a_risk_manager_may_go_ahead_and_it_is_noted(self):
        self.vehicle.driver_id = self.risk_manager.employee_id
        note = self._issue_note(user=self.risk_manager).picking_id.sudo()
        self.assertEqual(note.state, 'done')
        self.assertTrue(note.message_ids.filtered(
            lambda m: 'over the credit limit' in (m.body or '')))

    def test_an_offline_delivery_is_held_never_lost(self):
        capture = self.Capture.with_user(self.driver_user)._receive(self._payload(), self.driver)
        self.assertEqual(capture.state, 'held')
        self.assertIn('over the credit limit', capture.hold_reason)
        self.assertTrue(capture.picking_id, 'the note exists')
        self.assertNotEqual(capture.picking_id.state, 'done')

    def test_within_the_limit_nothing_changes(self):
        self.customer.credit_limit = 10000.0
        note = self._issue_note().picking_id.sudo()
        self.assertEqual(note.state, 'done')

    def test_dispatch_refuses_a_customer_over_his_limit(self):
        desk = self.env['res.users'].create({
            'name': 'Credit Desk', 'login': 'creditdesk@ksw.test',
            'group_ids': [(6, 0, [self.env.ref('KSW_water_delivery.group_water_dispatch').id])],
        })
        self.picking_type.write({'x_issue_mode': 'dispatch',
                                 'x_dispatcher_user_ids': [(6, 0, desk.ids)]})
        dispatch = self.env['ksw.water.dispatch'].with_user(desk).create({
            'picking_type_id': self.picking_type.id, 'partner_id': self.customer.id,
            'product_id': self.water.id, 'entered_qty': 1.0,
            'driver_user_id': self.driver_user.id})
        with self.assertRaisesRegex(UserError, 'over the credit limit'):
            dispatch.action_dispatch()
