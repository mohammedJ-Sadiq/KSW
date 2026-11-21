from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestTedderCoupling(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)
        cls.coupler = Users.create({'name': 'CP Coupler', 'login': 'cp_coupler', 'group_ids': [(6, 0, [
            cls.env.ref('KSW_fleet.group_tedder_coupling').id])]})
        cls.plain = Users.create({'name': 'CP Plain', 'login': 'cp_plain', 'group_ids': [(6, 0, [
            cls.env.ref('base.group_user').id, cls.env.ref('hr.group_hr_user').id])]})
        cls.editor = Users.create({'name': 'CP Editor', 'login': 'cp_editor', 'group_ids': [(6, 0, [
            cls.env.ref('KSW_fleet.group_tedder_editor').id])]})
        cls.hr_editor = Users.create({'name': 'CP HR Editor', 'login': 'cp_hr_editor', 'group_ids': [(6, 0, [
            cls.env.ref('KSW_fleet.group_tedder_editor').id, cls.env.ref('hr.group_hr_user').id])]})
        cls.viewer = Users.create({'name': 'CP Viewer', 'login': 'cp_viewer', 'group_ids': [(6, 0, [
            cls.env.ref('base.group_user').id])]})
        V = cls.env['ksw.fleet.vehicle']
        cls.head1 = V.create({'name': 'CP-T1', 'vehicle_type': 'trailer'})
        cls.head2 = V.create({'name': 'CP-T2', 'vehicle_type': 'trailer'})
        cls.tedder1 = V.create({'name': 'تيدر CP1', 'vehicle_type': 'tank'})
        cls.tedder2 = V.create({'name': 'تيدر CP2', 'vehicle_type': 'tank'})

    def _couple(self, tedder, head, user=None):
        wizard = self.env['ksw.fleet.couple.wizard'].with_user(user or self.coupler).create(
            {'tedder_id': tedder.id, 'head_id': head.id})
        wizard.action_confirm()
        return wizard

    def test_only_the_permission_couples_and_it_is_immediate(self):
        with self.assertRaises(UserError):
            self.tedder1.with_user(self.plain)._ksw_couple(self.head1)
        with self.assertRaises(UserError):  # no editing the field around the action
            self.tedder1.with_user(self.plain).sudo().write({'x_head_id': self.head1.id})
        self._couple(self.tedder1, self.head1)
        self.assertEqual((self.tedder1.x_head_id, self.head1.x_tedder_id), (self.head1, self.tedder1))
        line = self.env['ksw.fleet.coupling'].search([('tedder_id', '=', self.tedder1.id)])
        self.assertEqual((line.head_id, line.user_id, line.date_to), (self.head1, self.coupler, False))

    def test_swap_lets_go_and_keeps_history(self):
        self._couple(self.tedder1, self.head1)
        # The head takes another tedder: the first one is let go, at once.
        wizard = self.env['ksw.fleet.couple.wizard'].with_user(self.coupler).create(
            {'tedder_id': self.tedder2.id, 'head_id': self.head1.id})
        self.assertIn('تيدر CP1', wizard.replaces)
        wizard.action_confirm()
        self.assertFalse(self.tedder1.x_head_id)
        self.assertEqual(self.head1.x_tedder_id, self.tedder2)
        # The tedder moves to another head.
        self._couple(self.tedder2, self.head2)
        self.assertFalse(self.head1.x_tedder_id)
        history = self.env['ksw.fleet.coupling'].search([('tedder_id', 'in', (self.tedder1 | self.tedder2).ids)])
        self.assertEqual(len(history), 3)
        self.assertEqual(len(history.filtered(lambda h: not h.date_to)), 1)
        self.tedder2.with_user(self.coupler).action_uncouple()
        self.assertFalse(self.head2.x_tedder_id)
        with self.assertRaises(UserError):
            history.with_user(self.coupler).unlink()

    def test_the_permission_alone_reaches_the_page(self):
        # A person with only this permission still has a place to do it.
        root = self.env.ref('KSW_fleet.menu_ksw_fleet_tedders_root')
        Menu = self.env['ir.ui.menu']
        self.assertIn(root.id, Menu.with_user(self.coupler)._visible_menu_ids())
        self.assertNotIn(root.id, Menu.with_user(self.plain)._visible_menu_ids())
        action = self.env.ref('KSW_fleet.action_ksw_fleet_tedders')
        tedders = self.env['ksw.fleet.vehicle'].with_user(self.coupler).search(eval(action.domain))
        self.assertIn(self.tedder1, tedders)
        tedders.web_read({'name': {}, 'x_head_id': {'fields': {'display_name': {}}}, 'x_head_plate': {}})

    def test_editor_edits_tedders_only_and_never_couples(self):
        self.tedder1.with_user(self.editor).write({'tank_number': 'TK-77'})
        self.assertEqual(self.tedder1.tank_number, 'TK-77')
        with self.assertRaises(UserError):
            self.head1.with_user(self.editor).write({'tank_number': 'X'})
        with self.assertRaises(UserError):
            self.tedder1.with_user(self.editor)._ksw_couple(self.head1)
        self.assertFalse(self.tedder1.with_user(self.editor).x_can_couple)
        # An HR officer who also holds it keeps editing every vehicle.
        self.head1.with_user(self.hr_editor).write({'tank_number': 'H-1'})
        self.assertIn(self.env.ref('KSW_fleet.menu_ksw_fleet_tedders_root').id,
                      self.env['ir.ui.menu'].with_user(self.editor)._visible_menu_ids())

    def test_coupler_does_not_edit_tedder_data(self):
        # Refused by the access rights, or first by Water Delivery's own guard.
        try:
            self.tedder1.with_user(self.coupler).write({'tank_number': 'X'})
        except (AccessError, UserError):
            pass
        else:
            self.fail('The coupler edited tedder data.')

    def test_anyone_who_opens_the_vehicle_sees_its_coupling(self):
        self._couple(self.tedder1, self.head1)
        data = self.head1.with_user(self.viewer).web_read({
            'x_tedder_id': {'fields': {'display_name': {}}}, 'x_coupled_since': {},
            'x_coupling_ids': {'fields': {'tedder_id': {'fields': {'display_name': {}}}}}})[0]
        self.assertEqual(data['x_tedder_id']['id'], self.tedder1.id)
        self.assertTrue(data['x_coupled_since'])
        self.assertNotIn(self.env.ref('KSW_fleet.menu_ksw_fleet_tedders_root').id,
                         self.env['ir.ui.menu'].with_user(self.viewer)._visible_menu_ids())

    def test_hr_officer_without_the_permission_cannot_edit_a_tedder(self):
        # Mishal's case (2026-11-21): HR officer + coupler, no «Edit Tedder Data».
        hr_coupler = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'CP HR Coupler', 'login': 'cp_hr_coupler', 'group_ids': [(6, 0, [
                self.env.ref('hr.group_hr_user').id, self.env.ref('KSW_fleet.group_tedder_coupling').id])]})
        with self.assertRaises(UserError):
            self.tedder1.with_user(hr_coupler).write({'tank_number': 'X'})
        self.assertFalse(self.tedder1.with_user(hr_coupler).x_can_edit_vehicle)
        # He still couples, and still edits the other vehicles as before.
        self._couple(self.tedder1, self.head1, user=hr_coupler)
        self.head1.with_user(hr_coupler).write({'tank_number': 'H-2'})
        self.assertTrue(self.head1.with_user(hr_coupler).x_can_edit_vehicle)
        self.assertTrue(self.tedder1.with_user(self.editor).x_can_edit_vehicle)
