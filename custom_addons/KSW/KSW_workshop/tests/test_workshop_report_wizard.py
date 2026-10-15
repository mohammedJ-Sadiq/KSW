"""The four readable reports: by date, by vehicle, by repair type, by spare part.

The test database carries the real 17k-request history, so every fixture
lives in 2001 and every report here is run over 2001 only.
"""
from datetime import date, datetime

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestWorkshopReportWizard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)
        cls.reviewer = Users.create({
            'name': 'RW Reviewer', 'login': 'rw_reviewer', 'tz': 'Asia/Riyadh',
            'group_ids': [(6, 0, [cls.env.ref('KSW_workshop.group_workshop_reviewer').id])]})
        cls.requester = cls.env['hr.employee'].create({'name': 'RW Requester'})
        cls.truck = cls.env['ksw.fleet.vehicle'].create({'name': 'RW-T1', 'vehicle_type': 'isuzu'})
        cls.other_truck = cls.env['ksw.fleet.vehicle'].create({'name': 'RW-T2', 'vehicle_type': 'isuzu'})
        cls.oil = cls.env.ref('KSW_workshop.request_type_oil_filters')
        cls.brakes = cls.env.ref('KSW_workshop.request_type_brakes')
        cls.part = cls.env['ksw.workshop.part'].create({
            'name': 'RW-FLT-1', 'description': 'فلتر زيت', 'standard_cost': 30.0})

        # 2001-03-05 21:30 UTC is 2001-03-06 00:30 in Riyadh: it belongs to the 6th.
        cls.r_oil = cls._make(cls.truck, 'غيار زيت', datetime(2001, 3, 5, 21, 30),
                              repairs_parts='تم تغيير فلتر زيت')
        cls.r_both = cls._make(cls.truck, 'غيار زيت وفرامل', datetime(2001, 3, 20, 8, 0))
        cls.r_other = cls._make(cls.other_truck, 'فرامل', datetime(2001, 4, 2, 8, 0))
        cls.r_open = cls._make(cls.other_truck, 'غيار زيت', datetime(2001, 4, 3, 8, 0), state='new')
        cls.r_rejected = cls._make(cls.truck, 'غيار زيت', datetime(2001, 4, 4, 8, 0), state='rejected')

        Line = cls.env['ksw.workshop.part.line']
        cls.line_a = Line.create({'request_id': cls.r_oil.id, 'part_id': cls.part.id, 'quantity': 2,
                                  'unit_cost': 30.0, 'issue_date': date(2001, 3, 6)})
        cls.line_b = Line.create({'request_id': cls.r_other.id, 'part_id': cls.part.id, 'quantity': 1,
                                  'unit_cost': 36.0, 'issue_date': date(2001, 4, 2)})
        # Lines are in; now close the repairs.
        cls.env.cr.execute("UPDATE ksw_workshop_request SET state = 'completed' WHERE id IN %s",
                           (tuple(r.id for r in (cls.r_oil, cls.r_both, cls.r_other)),))
        cls.env.invalidate_all()
        cls.unused_part = cls.env['ksw.workshop.part'].create({'name': 'RW-BRK-1', 'description': 'قماش فرامل'})

    @classmethod
    def _make(cls, vehicle, description, created, state='completed', **vals):
        request = cls.env['ksw.workshop.request'].create({
            'vehicle_type': vehicle.vehicle_type, 'vehicle_id': vehicle.id,
            'employee_id': cls.requester.id, 'driver_id': cls.requester.id,
            'description': description, **vals})
        # Part lines are only accepted while the repair is in progress.
        cls.env.cr.execute(
            "UPDATE ksw_workshop_request SET create_date = %s, state = %s, completion_date = %s WHERE id = %s",
            (created, 'in_progress' if state == 'completed' else state,
             created if state == 'completed' else None, request.id))
        request.invalidate_recordset()
        return request

    def _wizard(self, user=None, **vals):
        Wizard = self.env['ksw.workshop.report.wizard'].with_user(user or self.reviewer)
        report_type = vals.get('report_type', 'date')
        defaults = Wizard.with_context(default_report_type=report_type).default_get(list(Wizard._fields))
        defaults.update({'date_from': date(2001, 1, 1), 'date_to': date(2001, 12, 31)})
        defaults.update(vals)
        return Wizard.create(defaults)

    def _render(self, wizard, user=None):
        html, _type = self.env['ir.actions.report'].with_user(user or self.reviewer)._render_qweb_html(
            'KSW_workshop.action_report_workshop_html', wizard.ids)
        return html.decode()

    # ------------------------------------------------------------------
    # Defaults per menu
    # ------------------------------------------------------------------
    def test_each_menu_opens_on_its_own_defaults(self):
        Wizard = self.env['ksw.workshop.report.wizard'].with_user(self.reviewer)
        fields_list = list(Wizard._fields)
        by_date = Wizard.with_context(default_report_type='date').default_get(fields_list)
        self.assertEqual(by_date['group_by'], 'day')
        self.assertEqual(by_date['date_from'].day, 1, "By Date opens on the current month")
        vehicle = Wizard.with_context(default_report_type='vehicle').default_get(fields_list)
        self.assertEqual(vehicle['group_by'], 'month')
        self.assertEqual(vehicle['state_scope'], 'active', "a history includes the job in progress")
        self.assertFalse(vehicle.get('date_from'), "a history starts at the beginning")
        self.assertEqual(Wizard.with_context(default_report_type='spare_part').default_get(fields_list)['group_by'], 'month')

    # ------------------------------------------------------------------
    # By date
    # ------------------------------------------------------------------
    def test_date_report_uses_local_days(self):
        data = self._wizard(date_from=date(2001, 3, 6), date_to=date(2001, 3, 6), vehicle_id=self.truck.id).get_report_data()
        names = [row['name'] for g in data['groups'] for row in g['rows']]
        self.assertEqual(names, [self.r_oil.name], "21:30 UTC on the 5th is the 6th in Riyadh")

    def test_date_report_completed_only_by_default(self):
        data = self._wizard(group_by='none').get_report_data()
        names = {row['name'] for g in data['groups'] for row in g['rows']}
        self.assertIn(self.r_other.name, names)
        self.assertNotIn(self.r_open.name, names)
        self.assertNotIn(self.r_rejected.name, names)
        data = self._wizard(group_by='none', state_scope='active').get_report_data()
        names = {row['name'] for g in data['groups'] for row in g['rows']}
        self.assertIn(self.r_open.name, names)
        self.assertNotIn(self.r_rejected.name, names)

    def test_group_by_month_summary(self):
        data = self._wizard(group_by='month', vehicle_type='isuzu', state_scope='active').get_report_data()
        summary = data['tables'][0]
        counts = {row[0]: row[1] for row in summary['rows']}
        self.assertEqual(list(counts.values()), [2, 2], "March then April, in date order")
        self.assertEqual(summary['total'][1], 4)

    def test_summary_only(self):
        data = self._wizard(show_details=False).get_report_data()
        self.assertFalse(data['groups'])
        self.assertTrue(data['tables'])

    # ------------------------------------------------------------------
    # By vehicle
    # ------------------------------------------------------------------
    def test_defaults_follow_the_report_type_without_menu_context(self):
        # The vehicle form's button and any RPC caller create the wizard
        # without default_report_type in the context.
        wizard = self.env['ksw.workshop.report.wizard'].with_user(self.reviewer).create({
            'report_type': 'vehicle', 'vehicle_id': self.truck.id})
        self.assertFalse(wizard.date_from, "a vehicle history is not limited to this month")
        self.assertEqual(wizard.state_scope, 'active')
        wizard.report_type = 'date'
        wizard._onchange_report_type()
        self.assertTrue(wizard.date_from)
        self.assertEqual(wizard.group_by, 'day')

    def test_vehicle_report_needs_a_vehicle(self):
        with self.assertRaises(UserError):
            self._wizard(report_type='vehicle').action_view()

    def test_vehicle_history_is_that_vehicle_only(self):
        data = self._wizard(report_type='vehicle', vehicle_id=self.truck.id).get_report_data()
        names = {row['name'] for g in data['groups'] for row in g['rows']}
        self.assertEqual(names, {self.r_oil.name, self.r_both.name})
        kpis = dict(data['kpis'])
        self.assertEqual(kpis['Requests'], 2)
        self.assertIn('First Visit', kpis)
        # Legacy free-text parts show up on the row.
        row = next(r for g in data['groups'] for r in g['rows'] if r['name'] == self.r_oil.name)
        self.assertIn('فلتر زيت', row['parts'])

    def test_vehicle_form_opens_the_report_on_that_vehicle(self):
        action = self.truck.with_user(self.reviewer).action_workshop_history_report()
        self.assertEqual(action['context']['default_vehicle_id'], self.truck.id)

    # ------------------------------------------------------------------
    # By repair type
    # ------------------------------------------------------------------
    def test_repair_type_filter_and_multi_type_counting(self):
        wizard = self._wizard(report_type='repair_type', group_by='repair_type',
                              request_type_ids=[(6, 0, [self.oil.id, self.brakes.id])])
        data = wizard.get_report_data()
        by_type = {row[0]: row[1] for row in data['tables'][0]['rows']}
        self.assertEqual(by_type[self.oil.name], 2)     # r_oil, r_both
        self.assertEqual(by_type[self.brakes.name], 2)  # r_both, r_other
        self.assertTrue(data['multi_count_note'], "r_both is counted twice — the page must say so")

    def test_repair_type_only_selected(self):
        data = self._wizard(report_type='repair_type', group_by='none',
                            request_type_ids=[(6, 0, [self.brakes.id])]).get_report_data()
        names = {row['name'] for g in data['groups'] for row in g['rows']}
        self.assertEqual(names, {self.r_both.name, self.r_other.name})

    # ------------------------------------------------------------------
    # By spare part
    # ------------------------------------------------------------------
    def _part_field(self):
        # With KSW_workshop_stock installed, the wizard also offers stock items;
        # the pass-through item is still reported through part_id.
        return {'part_id': self.part.id}

    def test_spare_part_statistics(self):
        data = self._wizard(report_type='spare_part', **self._part_field()).get_report_data()
        kpis = dict(data['kpis'])
        self.assertEqual(kpis['Quantity Issued'], '3')
        self.assertEqual(kpis['Value Issued'], '96.00')
        self.assertEqual(kpis['Avg. Unit Cost'], '32.00')
        self.assertEqual(kpis['Vehicles'], 2)
        months = [row[1] for row in data['tables'][0]['rows']]
        self.assertEqual(months, ['2', '1'])

    def test_spare_part_ranking_without_a_part(self):
        data = self._wizard(report_type='spare_part').get_report_data()
        ranking = next(t for t in data['tables'] if t['header'][0] == 'Item No.')
        row = next(r for r in ranking['rows'] if r[0] == 'RW-FLT-1')
        self.assertEqual(row[2], '3')

    def test_text_mentions_are_separate_from_quantities(self):
        data = self._wizard(report_type='spare_part', text_search='فلتر زيت', **self._part_field()).get_report_data()
        self.assertEqual(data['mentions']['count'], 1)
        self.assertEqual(dict(data['kpis'])['Quantity Issued'], '3', "mentions never add to quantities")

    def test_text_mentions_without_issues(self):
        # Most of the history: the part was never itemised, only written down.
        data = self._wizard(report_type='spare_part', part_id=self.unused_part.id,
                            text_search='فرامل').get_report_data()
        self.assertTrue(data['no_lines'])
        self.assertFalse(data['empty'], "mentions alone are still a report")
        self.assertEqual(data['mentions']['count'], 2)  # r_both, r_other

    # ------------------------------------------------------------------
    # Rendering as the reviewer (no HR rights): every report, both outputs
    # ------------------------------------------------------------------
    def test_every_report_renders_for_the_reviewer(self):
        cases = [
            {'report_type': 'date'},
            {'report_type': 'vehicle', 'vehicle_id': self.truck.id},
            {'report_type': 'repair_type', 'group_by': 'repair_type'},
            {'report_type': 'spare_part', 'text_search': 'فلتر', **self._part_field()},
            {'report_type': 'spare_part', 'group_by': 'vehicle'},
        ]
        for vals in cases:
            with self.subTest(**{k: v for k, v in vals.items() if k == 'report_type'}):
                html = self._render(self._wizard(**vals))
                self.assertIn('RW-T1', html)

    def test_empty_report_says_so(self):
        html = self._render(self._wizard(date_from=date(2000, 1, 1), date_to=date(2000, 1, 2)))
        self.assertIn('No workshop activity matches these filters', html)
