from odoo.exceptions import UserError
from odoo.tests import Form, TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestReportedBy(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.env.ref('base.main_company')
        cls.company_b = cls.env['res.company'].create({'name': 'Helpdesk Other Co'})
        # KSW "Employees / Employee" tier: direct hr.employee read, but its
        # record rule only exposes the user's own employee.
        group = cls.env.ref(
            'KSW_base_security.group_hr_employee_subordinate', raise_if_not_found=False,
        ) or cls.env.ref('base.group_user')
        cls.manager_user = cls.env['res.users'].create({
            'name': 'HD Manager', 'login': 'hd_manager_test',
            'company_id': cls.company_a.id,
            'company_ids': [(6, 0, [cls.company_a.id, cls.company_b.id])],
            'group_ids': [(6, 0, [group.id])],
        })
        # Employee record lives in company B while the user's active company is A.
        cls.manager = cls.env['hr.employee'].create({
            'name': 'HD Manager', 'user_id': cls.manager_user.id, 'company_id': cls.company_b.id,
        })
        cls.report = cls.env['hr.employee'].create({
            'name': 'HD Direct Report', 'parent_id': cls.manager.id, 'company_id': cls.company_b.id,
        })
        cls.stranger = cls.env['hr.employee'].create({'name': 'HD Stranger', 'company_id': cls.company_a.id})
        cls.category = cls.env['helpdesk.ticket.category'].search([('ticket_type', '=', 'incident')], limit=1)

    def _ticket_model(self):
        return self.env['helpdesk.ticket'].with_user(self.manager_user).with_context(
            allowed_company_ids=[self.company_a.id],
        )

    def _search_reporters(self, name=''):
        Employee = self.env['hr.employee'].with_user(self.manager_user).with_context(
            allowed_company_ids=[self.company_a.id], ksw_helpdesk_reporter=True,
        )
        domain = self._ticket_model()._domain_employee_id()
        return {r['id'] for r in Employee.web_name_search(name, {'display_name': {}}, domain)}

    def test_default_is_own_employee_across_companies(self):
        form = Form(self._ticket_model())
        self.assertEqual(form.employee_id, self.manager)

    def test_dropdown_lists_self_and_direct_reports_only(self):
        self.assertEqual(self._search_reporters(), {self.manager.id, self.report.id})
        self.assertEqual(self._search_reporters('Direct'), {self.report.id})

    def test_create_for_direct_report(self):
        form = Form(self._ticket_model())
        form.employee_id = self.report
        form.name = 'Printer jam'
        form.description = '<p>Paper stuck</p>'
        form.category_id = self.category
        ticket = form.save()
        self.assertEqual(ticket.sudo().employee_id, self.report)

    def test_create_for_stranger_is_refused(self):
        with self.assertRaises(UserError):
            self._ticket_model().create({
                'name': 'x', 'description': '<p>y</p>',
                'category_id': self.category.id, 'employee_id': self.stranger.id,
            })

