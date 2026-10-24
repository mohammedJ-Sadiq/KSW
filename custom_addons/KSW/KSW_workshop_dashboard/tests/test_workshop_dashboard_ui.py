from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestWorkshopDashboardUI(HttpCase):
    """A reviewer's Workshop app opens on the overview, which renders its figures with no JS error."""

    def test_reviewer_lands_on_the_overview(self):
        user = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'dash_ui_reviewer', 'login': 'dash_ui_reviewer', 'password': 'dash_ui_reviewer',
            'group_ids': [(6, 0, [self.env.ref('KSW_workshop.group_workshop_reviewer').id])]})
        self.assertTrue(user)
        root = self.env.ref('KSW_workshop.menu_ksw_workshop_root')
        self.browser_js(
            f'/odoo?menu_id={root.id}',
            "console.log('test successful')",
            ready="document.querySelectorAll('.o_ksw_dashboard .o_ksw_chart canvas').length > 0",
            login='dash_ui_reviewer', timeout=120)
        hub = self.env.ref('KSW_workshop_dashboard.hub_dashboard_workshop')
        self.browser_js(
            f'/odoo/dashboards?dashboard_id={hub.id}', "console.log('test successful')",
            ready="document.querySelectorAll('.o_spreadsheet_dashboard_action .o_ksw_hub_page .o_ksw_chart canvas')"
                  ".length > 0",
            login='dash_ui_reviewer', timeout=120)

