from lxml import etree

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMobileViews(TransactionCase):

    ACTIONS = [
        'KSW_attendance_sheet.action_ksw_attendance_sheet',
        'KSW_annual_leave.hr_leave_action_return_confirmation',
        'KSW_deduction.action_ksw_my_deductions',
        'KSW_deduction.action_ksw_my_loans',
        'hr_attendance.hr_attendance_management_action',
        'KSW_payroll.action_ksw_my_revision_requests',
        'KSW_payroll.action_ksw_revision_requests_todo',
        'KSW_payroll.action_ksw_revision_requests_all',
        'KSW_water_delivery.action_water_location_requests',
        'KSW_water_delivery.action_water_captures',
        'KSW_water_delivery.action_water_tankers',
        'KSW_commissions.action_ksw_pay_run',
    ]

    def test_desktop_still_opens_list_and_phone_has_cards(self):
        for xmlid in self.ACTIONS:
            action = self.env.ref(xmlid)
            modes = [mode for _view, mode in action.views]
            self.assertEqual(modes[0], 'list', xmlid)
            self.assertIn('kanban', modes, xmlid)
            if 'form' in action.view_mode or action.view_ids.filtered(lambda v: v.view_mode == 'form'):
                self.assertIn('form', modes, xmlid)
            self.assertEqual(action.mobile_view_mode, 'kanban', xmlid)

    def test_no_cards_on_actions_grouped_by_default(self):
        # Grouped cards are one column per group, swiped sideways: on a phone
        # those actions stay grouped lists with priority columns instead.
        for xmlid in self.ACTIONS:
            context = self.env.ref(xmlid).context or ''
            self.assertNotIn('search_default_group', context, xmlid)
            self.assertNotIn('search_default_grp', context, xmlid)
            self.assertNotIn('groupby', context, xmlid)

    def test_desktop_keeps_the_actions_own_list_view(self):
        # A view_ids row wins over action.view_id for its mode, so a list row
        # without a view would silently swap the action's own list on desktop.
        for xmlid in self.ACTIONS:
            action = self.env.ref(xmlid)
            if action.view_id.type == 'list':
                self.assertEqual(action.views[0], (action.view_id.id, 'list'), xmlid)

    def test_card_views_render(self):
        for xmlid in self.ACTIONS:
            action = self.env.ref(xmlid)
            kanban_id = dict((m, v) for v, m in action.views)['kanban']
            res = self.env[action.res_model].get_views([(kanban_id, 'kanban')])
            self.assertIn('kanban', res['views'], xmlid)

    def test_leave_card_has_step_buttons(self):
        arch = self.env['hr.leave'].get_views(
            [(self.env.ref('hr_holidays.hr_leave_view_kanban').id, 'kanban')]
        )['views']['kanban']['arch']
        doc = etree.fromstring(arch)
        for method in ('action_dm_approve', 'action_hr_approve',
                       'action_gm_initial_approve', 'action_acc_approve',
                       'action_gm_final_approve'):
            self.assertTrue(doc.xpath(f"//button[@name='{method}']"), method)

    def test_entries_mark_low_priority_columns(self):
        arch = self.env['ksw.pay.batch'].get_views(
            [(self.env.ref('KSW_commissions.view_ksw_pay_batch_form').id, 'form')]
        )['views']['form']['arch']
        doc = etree.fromstring(arch)
        entries = doc.xpath("//page[@name='entries']/field[@name='entry_ids']/list")[0]

        def classes(name):
            return (entries.xpath(f"./field[@name='{name}']")[0].get('class') or '').split()

        self.assertIn('ksw_m_hide', classes('rate'))
        self.assertIn('ksw_m_hide', classes('x_overlap_note'))
        # What a supervisor types must stay editable on a phone.
        for name in ('employee_id', 'option_id', 'date', 'quantity', 'location_id', 'reason', 'amount'):
            self.assertNotIn('ksw_m_hide', classes(name), name)

    def test_card_labels_are_arabic_in_arabic(self):
        if not self.env['res.lang']._lang_get('ar_001'):
            self.skipTest('Arabic not installed')
        views = {
            'KSW_mobile_ui.ksw_attendance_sheet_view_kanban_mobile': 'بانتظار',
            'KSW_mobile_ui.hr_leave_view_kanban_mobile_steps': 'موافقة',
            'KSW_mobile_ui.hr_leave_view_kanban_return_mobile': 'تأكيد المباشرة',
            'KSW_mobile_ui.ksw_deduction_view_kanban_my_mobile': 'الرصيد المتبقي',
            'KSW_mobile_ui.hr_attendance_view_kanban_mobile': 'مشمول بإجازة',
            'KSW_mobile_ui.ksw_payslip_revision_request_view_kanban_mobile': 'الفرق',
            'KSW_mobile_ui.view_water_location_request_kanban_mobile': 'فتح الخريطة',
            'KSW_mobile_ui.view_water_capture_kanban_mobile': 'إعادة المحاولة',
            'KSW_mobile_ui.view_water_tanker_kanban_mobile': 'بدون سائق',
            'KSW_mobile_ui.view_ksw_pay_run_kanban_mobile': 'الإجمالي المستحق',
        }
        for xmlid, term in views.items():
            arch = self.env.ref(xmlid).with_context(lang='ar_001').arch_db
            self.assertIn(term, arch, xmlid)

    def test_grouped_water_notes_open_as_a_list_on_a_phone(self):
        action = self.env.ref('KSW_water_delivery.action_water_delivery_all_notes')
        self.assertEqual(action.mobile_view_mode, 'list')
        self.assertEqual(action.views[0][1], 'list')

    def test_line_tables_carry_their_column_labels(self):
        # The phone card layout for line tables (mobile.scss) reads each
        # cell's data-label; the template extension must ship in the bundle.
        from odoo.tools.misc import file_open
        with file_open('KSW_mobile_ui/static/src/xml/list_renderer_mobile.xml') as f:
            self.assertIn('t-att-data-label', f.read())
        bundle = self.env['ir.qweb']._get_asset_bundle('web.assets_backend', debug_assets=False)
        self.assertTrue(any(
            'list_renderer_mobile.xml' in getattr(a, 'url', '') for a in bundle.templates
        ))
