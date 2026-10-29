"""Cost centres on water sales: tanker + driver, or the branch on a pickup."""
from odoo.tests import tagged

from .test_water_delivery import WaterDeliveryCommon


def _plans(env):
    """Three top-level plans, as on a database after the split."""
    Plan = env['account.analytic.plan']
    Account = env['account.analytic.account']
    vehicles = Plan.create({'name': 'Test Vehicle CC'})
    branches = Plan.create({'name': 'Test Branch CC'})
    employees = Plan.create({'name': 'Test Employee CC'})
    return (Account.create({'name': 'T-100 tanker', 'plan_id': vehicles.id}),
            Account.create({'name': 'Nabiya branch', 'plan_id': branches.id}),
            Account.create({'name': 'Driver One', 'plan_id': employees.id}))


@tagged('post_install', '-at_install')
class TestWaterAnalytic(WaterDeliveryCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tanker_cc, cls.branch_cc, cls.driver_cc = _plans(cls.env)
        cls.vehicle.x_analytic_account_id = cls.tanker_cc
        cls.picking_type.x_analytic_account_id = cls.branch_cc
        cls.driver.sudo().x_analytic_account_id = cls.driver_cc

    @staticmethod
    def _key(*accounts):
        return ','.join(str(a.id) for a in accounts)

    def _month_end_line(self, note):
        invoice = note._water_create_invoice()
        return invoice.invoice_line_ids.filtered(
            lambda l: l.sale_line_ids & note.sale_id.order_line)

    def test_a_delivery_carries_its_tanker_and_driver(self):
        note = self._issue_note().picking_id.sudo()
        line = self._month_end_line(note)
        self.assertEqual(line.analytic_distribution, {self._key(
            self.tanker_cc, self.branch_cc, self.driver_cc): 100.0})

    def test_each_note_keeps_its_own_tags_on_one_invoice(self):
        first = self._issue_note().picking_id.sudo()
        self.driver.sudo().x_analytic_account_id = False
        second = self._issue_note().picking_id.sudo()
        invoice = (first | second)._water_create_invoice()
        tags = {l.sale_line_ids.order_id.picking_ids.id: l.analytic_distribution
                for l in invoice.invoice_line_ids if l.sale_line_ids}
        self.assertEqual(tags[first.id], {self._key(self.tanker_cc, self.branch_cc, self.driver_cc): 100.0})
        self.assertEqual(tags[second.id], {self._key(self.tanker_cc, self.branch_cc): 100.0})

    def test_branch_and_vehicle_in_one_plan_keep_the_vehicle(self):
        """A database still on one Cost Centres plan: no double count."""
        self.branch_cc.plan_id = self.tanker_cc.plan_id
        note = self._issue_note().picking_id.sudo()
        self.assertEqual(self._month_end_line(note).analytic_distribution,
                         {self._key(self.tanker_cc, self.driver_cc): 100.0})

    def test_no_cost_centres_no_tags(self):
        self.vehicle.x_analytic_account_id = False
        self.picking_type.x_analytic_account_id = False
        self.driver.sudo().x_analytic_account_id = False
        note = self._issue_note().picking_id.sudo()
        self.assertFalse(self._month_end_line(note).analytic_distribution)
