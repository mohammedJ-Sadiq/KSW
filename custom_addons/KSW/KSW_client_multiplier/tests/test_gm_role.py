"""The Water Delivery General Manager role (Oct 2026).

Read-only on every water screen, plus deciding the multiplier changes addressed
to him. Checked the way the browser loads a screen -- get_views, then
web_search_read / web_read with exactly the fields the view asks for -- since
a group-restricted field only fails there (KSW gotchas #5, #31).
"""
from lxml import etree

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged
from odoo.tools.safe_eval import datetime, safe_eval

ACTIONS = [
    'KSW_water_delivery.action_water_delivery_all_notes',
    'KSW_water_delivery.action_water_captures',
    'KSW_water_delivery.action_water_dispatches',
    'KSW_water_delivery.action_water_location_requests',
    'KSW_water_delivery.action_water_tankers',
    'KSW_water_delivery.action_water_rates',
    'KSW_client_multiplier.action_client_multipliers',
    'KSW_client_multiplier.action_multiplier_requests',
    'KSW_client_multiplier.action_multiplier_requests_to_approve',
]


@tagged('post_install', '-at_install')
class TestWaterGmRole(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.gm = new_test_user(cls.env, 'water_gm_only',
                               groups='base.group_user,KSW_water_delivery.group_water_gm')

    def _view_fields(self, arch):
        root = etree.fromstring(arch)
        # Top-level fields only: those inside an embedded x2many view belong
        # to the comodel and are read through the x2many itself.
        return {
            node.get('name') for node in root.iter('field')
            if not any(a.tag in ('list', 'form', 'kanban') for a in node.iterancestors()
                       if a is not root)
        }

    def test_the_gm_can_open_every_water_screen(self):
        for xmlid in ACTIONS:
            action = self.env.ref(xmlid)
            Model = self.env[action.res_model].with_user(self.gm)
            modes = [m for m in (action.view_mode or 'list').split(',')
                     if m in ('list', 'form', 'kanban')]
            # The screen's own views, as the menu opens them.
            own = {v.view_mode: v.view_id.id for v in action.view_ids}
            if action.view_id:
                own.setdefault(action.view_mode.split(',')[0], action.view_id.id)
            views = Model.get_views([(own.get(m, False), m) for m in modes])
            domain = safe_eval(action.domain or '[]', {
                'uid': self.gm.id, 'user': self.gm, 'datetime': datetime})
            for mode in modes:
                fields = self._view_fields(views['views'][mode]['arch'])
                spec = {f: {} for f in fields if f in Model._fields}
                with self.subTest(action=xmlid, view=mode):
                    result = Model.web_search_read(domain, spec, limit=20)
                    if result['records'] and mode == 'form':
                        Model.browse(result['records'][0]['id']).web_read(spec)

    def test_the_gm_sees_the_menus(self):
        visible = self.env['ir.ui.menu'].with_user(self.gm)._visible_menu_ids()
        for xmlid in ('KSW_water_delivery.menu_water_delivery_root',
                      'KSW_water_delivery.menu_water_delivery_all_notes',
                      'KSW_water_delivery.menu_water_delivery_rates',
                      'KSW_client_multiplier.menu_water_client_multipliers',
                      'KSW_client_multiplier.menu_water_multiplier_approvals'):
            self.assertIn(self.env.ref(xmlid).id, visible, xmlid)
        for xmlid in ('KSW_water_delivery.menu_water_delivery_new',
                      'KSW_water_delivery.menu_water_delivery_invoice'):
            self.assertNotIn(self.env.ref(xmlid).id, visible, xmlid)

    def test_the_gm_changes_nothing(self):
        item = self.env['product.pricelist.item'].search([], limit=1)
        if item:
            with self.assertRaises(AccessError):
                item.with_user(self.gm).write({'fixed_price': 1.0})
        picking = self.env['stock.picking'].search([('x_is_water_delivery', '=', True)], limit=1)
        if picking:
            with self.assertRaises(AccessError):
                picking.with_user(self.gm).write({'note': 'x'})

    def test_the_gm_reads_water_notes_only(self):
        Picking = self.env['stock.picking'].with_user(self.gm)
        self.assertFalse(Picking.search([('x_is_water_delivery', '=', False)], limit=1))

    def test_a_gm_cannot_raise_a_change_for_himself(self):
        """GM includes Supervisor, so he may fill in a request, but one that
        would land on his own desk is refused at Submit."""
        gm_emp = self.env['hr.employee'].create({'name': 'Water GM', 'user_id': self.gm.id})
        dept = self.env['hr.department'].create({'name': 'GM Dept', 'x_gm_id': gm_emp.id})
        gm_emp.department_id = dept
        customer = self.env['ksw.bas.customer'].create({'bas_code': '1203ZZ990', 'name_ar': 'x'})
        client = self.env['ksw.client.multiplier'].create({'customer_id': customer.id})
        self.env['ksw.client.multiplier.rate'].create({
            'client_id': client.id, 'multiplier': 1.0, 'source': 'opening'})
        req = self.env['ksw.client.multiplier.request'].with_user(self.gm).create({
            'client_id': client.id, 'new_multiplier': 2.0, 'reason': 'test'})
        with self.assertRaises(UserError):
            req.with_user(self.gm).action_submit()

    def test_the_gm_includes_supervisor_and_dispatcher(self):
        self.assertTrue(self.gm.has_group('KSW_water_delivery.group_water_dispatcher'))
        self.assertTrue(self.gm.has_group('KSW_water_delivery.group_water_dispatch'))
        self.assertFalse(self.gm.has_group('KSW_water_delivery.group_water_driver'))

    def test_the_gm_sees_everything_not_just_a_team(self):
        """He holds Supervisor too; its "his team" rules must not narrow him."""
        for model in ('ksw.water.capture', 'ksw.water.location.request', 'ksw.water.dispatch'):
            Model = self.env[model]
            with self.subTest(model=model):
                self.assertEqual(Model.with_user(self.gm).search_count([]),
                                 Model.sudo().search_count([]))
        self.assertEqual(
            self.env['stock.picking'].with_user(self.gm).search_count([('x_is_water_delivery', '=', True)]),
            self.env['stock.picking'].sudo().search_count([('x_is_water_delivery', '=', True)]))
