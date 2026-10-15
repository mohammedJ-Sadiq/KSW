"""The interactive reports, driven in a real browser.

What these reports are FOR is the clicking: expand a group, open the repair
behind a row, drill from a pivot cell into the rows behind it. None of that
exists in a shell probe, so it is tested where it happens.

Needs websocket-client (project .venv only; with odoo19env the test skips).
Set KSW_SCREENSHOTS=1 to keep a screenshot of each report: the harness only
captures on a console error, so that mode ends each test with one (and the
tests then report as failed — look at the images, not the verdict).
"""
import os

from odoo.tests import HttpCase, new_test_user, tagged


@tagged('post_install', '-at_install')
class TestWorkshopReportsUI(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        new_test_user(cls.env, login='ws_ui_reviewer', groups='KSW_workshop.group_workshop_reviewer',
                      tz='Asia/Riyadh')
        requester = cls.env['hr.employee'].create({'name': 'UI Requester'})
        cls.vehicle = vehicle = cls.env['ksw.fleet.vehicle'].create({'name': 'UI-T1', 'vehicle_type': 'isuzu'})
        request = cls.env['ksw.workshop.request'].create({
            'vehicle_type': 'isuzu', 'vehicle_id': vehicle.id, 'employee_id': requester.id,
            'driver_id': requester.id, 'description': 'UI test oil change',
            'work_statement': 'Changed oil', 'repairs_parts': 'UI oil filter'})
        request.write({'state': 'completed'})

    def _run(self, url, steps, name):
        code = """
            (async () => {
                const wait = async (sel, n = 200) => {
                    for (let i = 0; i < n; i++) {
                        const el = document.querySelector(sel);
                        if (el) return el;
                        await new Promise(r => setTimeout(r, 100));
                    }
                    throw new Error('not found: ' + sel);
                };
                %s
                console.%s;
            })().catch(e => console.error(e.message));
        """ % (steps, "error('KSW screenshot %s')" % name if os.environ.get('KSW_SCREENSHOTS')
                 else "log('test successful')")
        self.browser_js(url, code, login='ws_ui_reviewer', timeout=120)

    def _open(self, xmlid, steps):
        self._run(f'/odoo/action-{self.env.ref(xmlid).id}', steps, xmlid.split('.')[-1])

    def test_vehicle_history_expands_and_opens_a_repair(self):
        # The path a user takes: the truck, its Workshop Visits, a year, a repair.
        self._run(f'/odoo/ksw.fleet.vehicle/{self.vehicle.id}', """
            (await wait('button[name="action_view_workshop_requests"]')).click();
            (await wait('.o_group_header')).click();
            const row = await wait('.o_data_row');
            if (!row.textContent.includes('UI oil filter')) throw new Error('row lacks the parts');
            if (!row.textContent.includes('Changed oil')) throw new Error('row lacks the work done');
            row.querySelector('td.o_data_cell').click();
            await wait('.o_form_view .o_field_widget[name="description"]');
        """, 'vehicle_history')

    def test_repairs_by_date_groups_by_day(self):
        self._open('KSW_workshop.action_ksw_workshop_report_date', """
            await wait('.o_list_view');
            (await wait('.o_group_header')).click();
            await wait('.o_data_row');
        """)

    def test_repairs_by_type_loads(self):
        self._open('KSW_workshop.action_ksw_workshop_report_type', """
            await wait('.o_group_header');
        """)

    def test_parts_history_loads(self):
        self._open('KSW_workshop.action_ksw_workshop_report_parts', """
            await wait('.o_list_view, .o_view_nocontent');
        """)
