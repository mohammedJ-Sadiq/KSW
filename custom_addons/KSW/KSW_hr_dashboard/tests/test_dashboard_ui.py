"""Browser test: the KSW HR Overview spreadsheet dashboard renders.

The dashboard's chart JSON was scripted directly (Path A) rather than built
in the Spreadsheet app's interactive editor -- that editor lives in the
Enterprise module ``spreadsheet_dashboard_edition``, which is not installed
in this Community checkout. A malformed figure only surfaces as a client-side
rendering error, so this must be checked with a real browser, not an
``odoo shell`` probe (Odoo 19 Pitfalls #40).

Needs the project venv -- `browser_js` requires `websocket-client`, which
only `.venv` has:

    .venv/bin/python odoo-bin -c KSW_dev.conf --http-port=18078 --test-enable \\
      --test-tags /KSW_hr_dashboard -u KSW_hr_dashboard --stop-after-init
"""
from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestDashboardUI(HttpCase):

    def test_dashboard_renders_for_hr_manager(self):
        """All 4 figures render, no 'corrupted spreadsheet' banner."""
        admin = self.env.ref('base.user_admin')
        self.assertTrue(admin.has_group('hr.group_hr_manager'))

        code = """
            (async () => {
                for (let i = 0; i < 60 && !odoo.isReady; i++) {
                    await new Promise((r) => setTimeout(r, 250));
                }
                let item = null;
                for (let i = 0; i < 40; i++) {
                    item = document.querySelector(
                        'li[data-name="KSW HR Overview"]');
                    if (item) { break; }
                    await new Promise((r) => setTimeout(r, 250));
                }
                if (!item) {
                    console.error('KSW HR Overview not found in the ' +
                                  'Dashboards sidebar');
                    return;
                }
                item.click();
                let errorBanner = null;
                let canvasCount = 0;
                for (let i = 0; i < 80; i++) {
                    errorBanner = document.querySelector(
                        '.dashboard-loading-status.error');
                    if (errorBanner) { break; }
                    canvasCount = document.querySelectorAll(
                        '.o_renderer canvas').length;
                    if (canvasCount >= 4) { break; }
                    await new Promise((r) => setTimeout(r, 250));
                }
                if (errorBanner) {
                    console.error('Dashboard failed to load: ' +
                                  errorBanner.textContent.trim());
                    return;
                }
                console.log('KSW_CANVAS_COUNT ' + canvasCount);
                if (canvasCount < 4) {
                    console.error('Expected 4 chart figures, found ' +
                                  canvasCount);
                    return;
                }
                console.log('test successful');
            })();
        """
        self.browser_js(
            '/odoo/dashboards', code,
            ready='odoo.isReady === true',
            login='admin', timeout=90,
        )

    def test_dashboard_hidden_from_non_manager(self):
        """A user with none of the granted groups never sees the tile."""
        employee = self.env['hr.employee'].create({'name': 'Dashboard UI Test Employee'})
        user = self.env['res.users'].create({
            'name': 'Dashboard UI Test User',
            'login': 'ksw_dashboard_ui_test_user',
            'password': 'ksw_dashboard_ui_test_user',
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        employee.user_id = user
        for xmlid in [
            'hr.group_hr_manager',
            'om_hr_payroll.group_hr_payroll_manager',
            'KSW_annual_leave.group_annual_leave_hr',
            'KSW_attendance_sheet.group_attendance_sheet_manager',
            'KSW_deduction.group_deduction_manager',
        ]:
            self.assertFalse(user.has_group(xmlid))

        code = """
            (async () => {
                for (let i = 0; i < 60 && !odoo.isReady; i++) {
                    await new Promise((r) => setTimeout(r, 250));
                }
                await new Promise((r) => setTimeout(r, 1500));
                const item = document.querySelector(
                    'li[data-name="KSW HR Overview"]');
                if (item) {
                    console.error('KSW HR Overview tile is visible to a ' +
                                  'user without any granted group');
                    return;
                }
                console.log('test successful');
            })();
        """
        self.browser_js(
            '/odoo/dashboards', code,
            ready='odoo.isReady === true',
            login='ksw_dashboard_ui_test_user', timeout=90,
        )
