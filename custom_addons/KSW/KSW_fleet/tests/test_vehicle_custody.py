from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestVehicleCustody(TransactionCase):
    """Custody (عهدة) is its own fact, separate from the operating driver,
    changed only through _ksw_set_custodian and kept as history."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ahmed = cls.env['hr.employee'].create({'name': 'CU Ahmed'})
        cls.khalid = cls.env['hr.employee'].create({'name': 'CU Khalid'})
        cls.vehicle = cls.env['ksw.fleet.vehicle'].create({'name': 'CU-1', 'vehicle_type': 'isuzu',
                                                           'driver_id': cls.ahmed.id})
        cls.hr_user = cls.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'CU HR', 'login': 'cu_hr',
            'group_ids': [(6, 0, [cls.env.ref('hr.group_hr_user').id])]})

    def test_seed_and_history(self):
        self.vehicle._ksw_seed_custodians()
        self.assertEqual(self.vehicle.x_custodian_id, self.ahmed)
        self.vehicle._ksw_set_custodian(self.khalid, source='HR12/TEST', date='2026-11-12')
        self.assertEqual(self.vehicle.x_custodian_id, self.khalid)
        history = self.vehicle.x_custody_ids.sorted('id')
        self.assertEqual(history.mapped('employee_id'), self.ahmed | self.khalid)
        self.assertEqual(str(history[0].date_to), '2026-11-12')
        self.assertFalse(history[1].date_to)
        self.assertEqual(history[1].source, 'HR12/TEST')

    def test_only_the_handover_changes_custody(self):
        with self.assertRaises(UserError):
            self.vehicle.with_user(self.hr_user).write({'x_custodian_id': self.khalid.id})
        # The operating driver stays free for dispatch to move.
        self.vehicle.with_user(self.hr_user).write({'driver_id': self.khalid.id})
        self.assertEqual(self.vehicle.driver_id, self.khalid)
        self.vehicle._ksw_set_custodian(self.ahmed)
        with self.assertRaises(UserError):
            self.vehicle.x_custody_ids.with_user(self.hr_user).write({'note': 'x'})

    def test_custody_can_be_nobodys(self):
        self.vehicle._ksw_set_custodian(self.ahmed)
        self.vehicle._ksw_set_custodian(self.env['hr.employee'])
        self.assertFalse(self.vehicle.x_custodian_id)
        self.assertFalse(self.vehicle.x_custody_ids.filtered('is_current'))
