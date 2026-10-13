from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestRequesterGate(TransactionCase):
    """Only a Requester (or the workshop manager) raises a request; every
    employee still reads the ones he raised before the gate existed."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)

        def _user(login, groups):
            user = Users.create({
                'name': login, 'login': login, 'email': f'{login}@gate.test',
                'group_ids': [(6, 0, [cls.env.ref(g).id for g in groups])]})
            cls.env['hr.employee'].create({'name': login, 'user_id': user.id})
            return user

        cls.plain = _user('gate_plain', ('base.group_user',))
        cls.requester = _user('gate_requester', ('base.group_user', 'KSW_workshop.group_workshop_requester'))
        cls.vehicle = cls.env['ksw.fleet.vehicle'].create({'name': 'GATE1', 'vehicle_type': 'isuzu'})

    def _vals(self):
        return {'vehicle_type': 'isuzu', 'vehicle_id': self.vehicle.id,
                'driver_id': self.requester.employee_id.id, 'description': 'Oil change'}

    def test_plain_employee_cannot_raise_a_request(self):
        Request = self.env['ksw.workshop.request'].with_user(self.plain)
        self.assertFalse(Request.has_access('create'))
        with self.assertRaises(AccessError):
            Request.create(self._vals())

    def test_requester_can_raise_a_request(self):
        request = self.env['ksw.workshop.request'].with_user(self.requester).create(self._vals())
        self.assertEqual(request.employee_id, self.requester.employee_id)

    def test_plain_employee_still_reads_his_old_requests(self):
        old = self.env['ksw.workshop.request'].sudo().create(
            dict(self._vals(), employee_id=self.plain.employee_id.id))
        self.assertEqual(old.with_user(self.plain).read(['name'])[0]['name'], old.name)
