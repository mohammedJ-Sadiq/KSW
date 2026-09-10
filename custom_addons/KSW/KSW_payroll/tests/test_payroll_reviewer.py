# -*- coding: utf-8 -*-
"""Tests for the Payroll "Reviewer (All)" access tier (Sep 2026).

Accounting review payslips but must not be able to produce or change one.
Before this tier the only group that saw the whole company was Officer, and
Officer is the payroll *operator* role: full CRUD on payslips, batches and the
salary rules, plus the WPS / bank export wizards.  The three read-only tiers
below it are scoped by hierarchy, so a reviewer who manages nobody saw exactly
one payslip — their own.

Two latent bugs surfaced while building it, both fixed here and both affecting
the pre-existing read-only tiers just as much:

* `hr.payslip.line._order` was `version_id, sequence`.  Ordering on a Many2one
  falls through to the comodel's `_order`, and `hr.version._order` is
  `date_version` — a field with `groups="hr.group_hr_user"`.  Reading
  `payslip.line_ids` therefore raised AccessError for anyone who was not an HR
  Officer.  An ordinary employee opening their own payslip saw no lines at all.
* The KSW payslip PDF read `employee_id.job_id` / `.department_id` / `.ssnid`,
  all of which live on `hr.version` and reach `hr.employee` through
  `_inherits` — so each dereferenced the HR-gated `hr.employee.version_id`
  and blew up inside QWeb.

Officer only ever escaped both because it implies `hr.group_hr_user`.
"""
from datetime import date

from odoo.exceptions import AccessError, UserError
from odoo.tools import mute_logger
from odoo.tests.common import TransactionCase


class TestPayrollReviewer(TransactionCase):
    """Reviewer (All): reads every payslip company-wide, changes nothing."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Reviewer Test Calendar', 'tz': 'Asia/Riyadh',
        })
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Reviewer Test Employee',
            'resource_calendar_id': cls.calendar.id,
        })
        cls.version = cls.employee.current_version_id
        cls.version.write({
            'name': 'Reviewer Test Version',
            'date_version': date(2025, 1, 1),
            'contract_date_start': date(2025, 1, 1),
            'resource_calendar_id': cls.calendar.id,
            'wage': 5000.0,
            'struct_id': cls.env.ref('om_hr_payroll.structure_base').id,
        })
        cls.employee._compute_current_version_id()

        Users = cls.env['res.users'].with_context(no_reset_password=True)

        def _mk(login, group_xmlid):
            return Users.create({
                'name': login, 'login': login, 'email': f'{login}@kswrev.test',
                'group_ids': [(6, 0, [cls.env.ref('base.group_user').id,
                                      cls.env.ref(group_xmlid).id])],
            })

        cls.user_reviewer = _mk('kswpay_reviewer',
                                'KSW_payroll.group_hr_payroll_reviewer')
        cls.user_officer = _mk('kswpay_officer',
                               'om_hr_payroll.group_hr_payroll_user')

        cls.slip = cls.env['hr.payslip'].create({
            'employee_id': cls.employee.id,
            'name': 'Reviewer Test Slip',
            'date_from': date(2026, 7, 1),
            'date_to': date(2026, 7, 31),
            'struct_id': cls.env.ref('om_hr_payroll.structure_base').id,
            'version_id': cls.version.id,
        })
        cls.slip.compute_sheet()

    # ------------------------------------------------------------------
    # The tier chain
    # ------------------------------------------------------------------

    def test_tier_sits_between_cascade_and_officer(self):
        """Reviewer implies every read-only tier below it, and Officer
        implies Reviewer — so the privilege dropdown stays a ladder and an
        Officer never loses anything by the new group existing."""
        rev = self.env.ref('KSW_payroll.group_hr_payroll_reviewer')
        officer = self.env.ref('om_hr_payroll.group_hr_payroll_user')
        cascade = self.env.ref('KSW_payroll.group_hr_payroll_supervisor_cascade')
        self.assertIn(cascade, rev.implied_ids | rev.all_implied_ids)
        self.assertIn(rev, officer.implied_ids | officer.all_implied_ids)
        self.assertTrue(30 < rev.sequence < officer.sequence)

    def test_reviewer_is_not_an_hr_officer(self):
        """The tier must NOT reach for hr.group_hr_user — widening HR access
        is exactly what it exists to avoid. Everything the reviewer needs
        from the employee record is read through sudo() at the point of use.
        """
        self.assertFalse(
            self.user_reviewer.has_group('hr.group_hr_user'))

    # ------------------------------------------------------------------
    # Read: everything, company-wide
    # ------------------------------------------------------------------

    def test_reviewer_sees_every_payslip(self):
        Slip = self.env['hr.payslip']
        self.assertEqual(
            Slip.with_user(self.user_reviewer).search_count([]),
            Slip.sudo().search_count([]),
            "Reviewer (All) must see every payslip, not a hierarchy slice.")

    def test_reviewer_reads_payslip_lines(self):
        """Regression: `hr.payslip.line._order` dereferenced the HR-gated
        `hr.version.date_version`, so reading the o2m raised AccessError —
        and an o2m is read inline by the parent's web_read, so the whole
        payslip form died with it."""
        slip = self.slip.with_user(self.user_reviewer)
        slip.invalidate_recordset()
        data = slip.web_read({
            'id': {},
            'line_ids': {'fields': {'name': {}, 'code': {}, 'total': {}}},
            'worked_days_line_ids': {'fields': {'code': {}}},
            'input_line_ids': {'fields': {'code': {}}},
        })
        self.assertTrue(data[0]['line_ids'],
                        "payslip lines must be readable by a reviewer")

    def test_reviewer_renders_payslip_pdf(self):
        """Regression: the KSW payslip template read job_id / department_id /
        ssnid off hr.employee, each of which dereferences the HR-gated
        version_id (pitfall #5)."""
        report = self.env.ref('om_hr_payroll.action_report_payslip')
        html, _dummy = report.with_user(self.user_reviewer)._render_qweb_html(
            'om_hr_payroll.action_report_payslip', self.slip.ids)
        self.assertIn(b'Payslip', html)

    def test_reviewer_reads_batches(self):
        run = self.env['hr.payslip.run'].create({'name': 'Reviewer Batch'})
        self.assertTrue(
            run.with_user(self.user_reviewer).read(['name']),
            "Reviewing a month means reviewing its run.")

    # ------------------------------------------------------------------
    # Write: nothing, anywhere
    # ------------------------------------------------------------------

    @mute_logger('odoo.addons.base.models.ir_model')
    def test_reviewer_cannot_change_a_payslip(self):
        slip = self.slip.with_user(self.user_reviewer)
        with self.assertRaises(AccessError):
            slip.write({'name': 'Edited'})
        with self.assertRaises(AccessError):
            slip.compute_sheet()
        with self.assertRaises(AccessError):
            slip.unlink()
        with self.assertRaises(AccessError):
            self.env['hr.payslip'].with_user(self.user_reviewer).create({
                'employee_id': self.employee.id, 'name': 'New',
                'date_from': date(2026, 8, 1), 'date_to': date(2026, 8, 31),
                'struct_id': self.env.ref('om_hr_payroll.structure_base').id,
                'version_id': self.version.id,
            })

    @mute_logger('odoo.addons.base.models.ir_model')
    def test_reviewer_cannot_confirm_a_payslip(self):
        """The one that actually pays people: confirming settles deduction
        installments and emails the PDF to the employee."""
        with self.assertRaises(AccessError):
            self.slip.with_user(self.user_reviewer).action_payslip_done()

    @mute_logger('odoo.addons.base.models.ir_model')
    def test_reviewer_cannot_touch_batches_or_rules_or_exports(self):
        run = self.env['hr.payslip.run'].create({'name': 'Reviewer Batch 2'})
        with self.assertRaises(AccessError):
            run.with_user(self.user_reviewer).write({'name': 'Edited'})
        with self.assertRaises(AccessError):
            run.with_user(self.user_reviewer).done_payslip_run()
        with self.assertRaises(AccessError):
            self.env['hr.salary.rule'].with_user(
                self.user_reviewer).search([], limit=1).write({'name': 'X'})
        with self.assertRaises(AccessError):
            self.env['ksw.bank.file.export.wizard'].with_user(
                self.user_reviewer).create({})

    # ------------------------------------------------------------------
    # Nothing on screen that would only ever error
    # ------------------------------------------------------------------

    def test_batch_action_buttons_hidden_from_reviewer(self):
        """A button a reviewer may click but never run is the dead-menu
        problem one level down. The statusbar is deliberately NOT hidden:
        a reviewer has to see whether the run is draft or closed."""
        import re
        Run = self.env['hr.payslip.run']

        def buttons(user):
            arch = Run.with_user(user).get_views(
                [(False, 'form')])['views']['form']['arch']
            return set(re.findall(r'<button[^>]*name="([^"]+)"', arch))

        rev_buttons = buttons(self.user_reviewer)
        off_buttons = buttons(self.user_officer)
        for name in ('done_payslip_run', 'close_payslip_run',
                     'action_open_export_wizard', 'action_refresh_bank_totals',
                     'action_clear_skip_log'):
            self.assertNotIn(name, rev_buttons)
            self.assertIn(name, off_buttons)
        # The read-only drill-down stays.
        self.assertIn('action_open_batch_payslips', rev_buttons)
        self.assertIn('state', self.env['hr.payslip.run'].with_user(
            self.user_reviewer).get_views(
                [(False, 'form')])['views']['form']['arch'])

    def test_reviewer_menus(self):
        """Payslips and batches, and nothing that configures payroll."""
        menus = self.env['ir.ui.menu'].with_user(
            self.user_reviewer).load_menus(False)
        names = {m['name'] for m in menus.values() if isinstance(m, dict)}
        self.assertIn('Employee Payslips', names)
        self.assertIn('Payslips Batches', names)
        self.assertNotIn('Salary Rules', names)
        self.assertNotIn('Salary Structures', names)
