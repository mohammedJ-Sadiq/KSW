"""The batch in the ledger, booked as BAS books salaries.

What the parallel run depends on: a month already exported to BAS is never
booked (start date); a batch confirmed one paying account at a time is booked
one confirmation at a time, each payslip once; and a problem with one employee
never holds up the others, nor HR closing the batch.
"""
from datetime import date
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo.tools import mute_logger

from odoo.addons.KSW_payroll.tests.test_bas_journal_entry import BasJournalPayrollCommon


class TestPayrollLedger(BasJournalPayrollCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        Account = env['account.account'].sudo()

        def acc(code, kind='expense'):
            return Account.search([('code', '=', code)], limit=1) or Account.create(
                {'code': code, 'name': 'Ledger test %s' % code, 'account_type': kind})
        cls.wage = acc('9919010001')
        cls.saudi_wage = acc('9919010010')
        cls.camp_wage = acc('9924010001')   # a branch: its own 4-digit group
        cls.housing = acc('9919010002')
        cls.payable = acc('9917010004', 'liability_current')
        params = env['ir.config_parameter'].sudo()
        params.set_param('ksw_payroll_ledger.start_date', '2026-03-01')
        params.set_param('ksw_payroll_ledger.suspense_account_id', False)
        cls.hayat_wage = acc('9925010019')
        cls.hayat_payable = acc('9917010008', 'liability_current')
        cls.hayat_current = acc('9908010010', 'asset_current')
        cls.kawther_current = acc('9901030003', 'liability_current')
        # The posting configuration, as the migration builds it from BAS.
        Group = env['ksw.payroll.posting.group'].sudo()
        cls.cat_housing = env['ksw.payroll.posting.category'].sudo().create(
            {'name': 'Ledger Housing', 'code': 'LEDGER_HRA'})
        cls.rule_hra.sudo().write({'x_posting_category_id': cls.cat_housing.id, 'x_ledger_account_id': False})
        cls.main_group = Group.create({'name': 'Ledger Main Office', 'map_ids': [
            (0, 0, {'account_id': cls.wage.id}),
            (0, 0, {'category_id': cls.cat_housing.id, 'account_id': cls.housing.id})]})
        cls.m_group = Group.create({'name': 'Ledger M', 'map_ids': [(0, 0, {'account_id': cls.saudi_wage.id})]})
        cls.camp_group = Group.create({'name': 'Ledger Camp', 'map_ids': [(0, 0, {'account_id': cls.camp_wage.id})]})
        cls.hayat_group = Group.create({'name': 'Ledger Hayat Factory',
                                        'map_ids': [(0, 0, {'account_id': cls.hayat_wage.id})]})
        Entity = env['ksw.payroll.entity'].sudo()
        Entity.search([('company_id', '=', env.company.id)]).write({'is_default': False})
        cls.kawther = Entity.create({'name': 'Ledger Kawther', 'is_default': True,
                                     'payable_account_id': cls.payable.id, 'default_group_id': cls.main_group.id})
        cls.hayat = Entity.create({'name': 'Ledger Hayat', 'payable_account_id': cls.hayat_payable.id,
                                   'default_group_id': cls.hayat_group.id})
        IC = env['ksw.payroll.intercompany'].sudo()
        IC.create({'payer_id': cls.kawther.id, 'staff_entity_id': cls.hayat.id, 'posting_group_id': cls.m_group.id})
        IC.create({'payer_id': cls.hayat.id, 'staff_entity_id': cls.kawther.id,
                   'payer_account_id': cls.kawther_current.id, 'staff_account_id': cls.hayat_current.id})
        Bank = env['res.partner.bank'].sudo()
        partner = env.company.partner_id
        cls.kawther_bank = Bank.create({'acc_number': 'LEDGER Kawther Cards', 'partner_id': partner.id,
                                        'x_ledger_entity_id': cls.kawther.id})
        cls.hayat_bank = Bank.create({'acc_number': 'LEDGER Hayat Cards', 'partner_id': partner.id,
                                      'x_ledger_entity_id': cls.hayat.id})
        cls.hayat_dept = env['hr.department'].sudo().create(
            {'name': 'Ledger Hayat', 'x_ledger_entity_id': cls.hayat.id})
        cls.hayat_sub = env['hr.department'].sudo().create(
            {'name': 'Ledger Hayat IST', 'parent_id': cls.hayat_dept.id})

    def _lines(self, move):
        return {(l.account_id, round(l.balance, 2)) for l in move.line_ids}

    def _confirm(self, slips=None):
        """Confirm as HR does, without recomputing the hand-built lines."""
        def done(recs):
            recs.write({'state': 'done'})
            return True
        with patch.object(type(self.env['hr.payslip']), 'action_payslip_done', done):
            self.batch._ksw_confirm_slips(slips)
        return self.batch.x_ledger_move_ids

    def test_01_earnings_deductions_and_net_as_bas_books_them(self):
        self._payslip([(self.rule_basic, 5000.0), (self.rule_hra, 1250.0),
                       (self.rule_gross, 6250.0), (self.rule_ded, -250.0),
                       (self.rule_net, 6000.0)])
        move = self._confirm()
        self.assertEqual(move.state, 'posted')
        self.assertEqual(move.journal_id.code, 'SAL')
        self.assertEqual(move.date, date(2026, 3, 31))
        # The absence is not a line of its own: what is paid is expensed, the
        # wage and the housing each by the same share (250 of 6,250 = 4%).
        self.assertEqual(self._lines(move), {
            (self.wage, 4800.0), (self.housing, 1200.0), (self.payable, -6000.0)})

    def test_02_nothing_before_the_start_date(self):
        self.env['ir.config_parameter'].sudo().set_param('ksw_payroll_ledger.start_date', '2026-04-01')
        self._payslip([(self.rule_basic, 5000.0), (self.rule_net, 5000.0)])
        self.assertFalse(self._confirm())
        self.assertFalse(self.batch.x_ledger_error)

    def test_03_no_start_date_posts_nothing(self):
        self.env['ir.config_parameter'].sudo().set_param('ksw_payroll_ledger.start_date', False)
        self._payslip([(self.rule_basic, 5000.0), (self.rule_net, 5000.0)])
        self.assertFalse(self._confirm())

    def test_04_separate_salary_account_and_department_wage_accounts(self):
        """3201010010 is the ticked employee's, not the Saudi national's."""
        saudi = self.env['hr.employee'].sudo().create(
            {'name': 'Ledger Saudi', 'country_id': self.env.ref('base.sa').id})
        dept = self.env['hr.department'].sudo().create(
            {'name': 'Ledger Camp', 'x_posting_group_id': self.camp_group.id})
        camp = self.env['hr.employee'].sudo().create({'name': 'Ledger Camp Man', 'department_id': dept.id})
        apart = self.env['hr.employee'].sudo().create({
            'name': 'Ledger Apart', 'department_id': dept.id, 'x_posting_group_id': self.m_group.id})
        self._payslip([(self.rule_basic, 7000.0), (self.rule_net, 7000.0)], employee=saudi)
        self._payslip([(self.rule_basic, 2000.0), (self.rule_net, 2000.0)], employee=camp)
        self._payslip([(self.rule_basic, 8250.0), (self.rule_net, 8250.0)], employee=apart)
        lines = self._lines(self._confirm())
        self.assertIn((self.wage, 7000.0), lines)
        self.assertIn((self.camp_wage, 2000.0), lines)
        self.assertIn((self.saudi_wage, 8250.0), lines)

    def _employee(self, name, bank, dept=None):
        return self.env['hr.employee'].sudo().create({
            'name': name, 'department_id': dept.id if dept else False,
            'x_salary_bank_account_id': bank.id})

    def _all_lines(self):
        return {(l.account_id, round(l.balance, 2)) for l in self.batch.x_ledger_move_ids.line_ids}

    def test_04b_hayat_staff_paid_by_hayat(self):
        """Al-Hayat's own: its wage account, allowances included, its payable."""
        worker = self._employee('Ledger Hayat Worker', self.hayat_bank, self.hayat_sub)
        self._payslip([(self.rule_basic, 4000.0), (self.rule_hra, 1000.0),
                       (self.rule_gosi, -390.0), (self.rule_net, 4610.0)], employee=worker)
        self.assertEqual(self._lines(self._confirm()), {
            (self.hayat_wage, 4610.0), (self.hayat_payable, -4610.0)})

    def test_04c_hayat_staff_paid_by_kawther(self):
        """Paid from Kawther's account: the separate salary account, unticked."""
        worker = self._employee('Ledger Hayat On Kawther', self.kawther_bank, self.hayat_dept)
        self._payslip([(self.rule_basic, 1500.0), (self.rule_hra, 300.0),
                       (self.rule_net, 1800.0)], employee=worker)
        self.assertEqual(self._lines(self._confirm()), {
            (self.saudi_wage, 1800.0), (self.payable, -1800.0)})

    def test_04d_kawther_staff_paid_by_hayat(self):
        """Expensed by Kawther, owed to Al-Hayat; Al-Hayat pays it against Kawther."""
        clerk = self._employee('Ledger Kawther On Hayat', self.hayat_bank)
        self._payslip([(self.rule_basic, 6745.0), (self.rule_hra, 500.0),
                       (self.rule_net, 7245.0)], employee=clerk)
        self.assertEqual(self._lines(self._confirm()), {
            (self.wage, 6745.0), (self.housing, 500.0),
            (self.hayat_current, -7245.0), (self.kawther_current, 7245.0),
            (self.hayat_payable, -7245.0)})

    def test_04e_separate_salary_account_takes_the_allowances(self):
        """BAS books a separate salary in one line, allowances included."""
        apart = self.env['hr.employee'].sudo().create(
            {'name': 'Ledger Apart Whole', 'x_posting_group_id': self.m_group.id})
        self._payslip([(self.rule_basic, 12000.0), (self.rule_hra, 2500.0),
                       (self.rule_net, 14500.0)], employee=apart)
        self.assertEqual(self._lines(self._confirm()), {
            (self.saudi_wage, 14500.0), (self.payable, -14500.0)})

    def test_04f_a_branch_books_the_allowances_on_its_own_account(self):
        """Jubail camp: all 10,000 on the camp, none on the main office's allowances."""
        dept = self.env['hr.department'].sudo().create(
            {'name': 'Ledger Jubail', 'x_posting_group_id': self.camp_group.id})
        man = self.env['hr.employee'].sudo().create({'name': 'Ledger Camp Cook', 'department_id': dept.id})
        self._payslip([(self.rule_basic, 6000.0), (self.rule_hra, 4000.0),
                       (self.rule_net, 10000.0)], employee=man)
        self.assertEqual(self._lines(self._confirm()), {
            (self.camp_wage, 10000.0), (self.payable, -10000.0)})

    def test_04g_an_absence_is_spread_over_wage_and_allowances(self):
        """Six days of thirty-one: 484 wage + 97 allowance, not 81 + the whole 500."""
        self._payslip([(self.rule_basic, 2500.0), (self.rule_hra, 500.0),
                       (self.rule_ded, -2419.0), (self.rule_net, 581.0)])
        self.assertEqual(self._lines(self._confirm()), {
            (self.wage, 484.0), (self.housing, 97.0), (self.payable, -581.0)})

    def test_04h_gosi_to_his_own_account_or_netted(self):
        """GOSI on his own account, a line of its own; without one, netted."""
        self.rule_gosi.sudo().x_ledger_deduction = 'own_account'
        own = self.env['account.account'].sudo().create(
            {'code': '9905010099', 'name': 'Ledger own account', 'account_type': 'asset_current'})
        with_acc = self.env['hr.employee'].sudo().create(
            {'name': 'Ledger GOSI Own', 'x_loan_acc_no': own.code})
        without = self.env['hr.employee'].sudo().create({'name': 'Ledger GOSI None'})
        self._payslip([(self.rule_basic, 8000.0), (self.rule_ded, -2612.0),
                       (self.rule_gosi, -780.0), (self.rule_net, 4608.0)], employee=with_acc)
        self._payslip([(self.rule_basic, 4000.0), (self.rule_gosi, -390.0),
                       (self.rule_net, 3610.0)], employee=without)
        move = self._confirm()
        own_lines = move.line_ids.filtered(lambda l: l.account_id == own)
        self.assertEqual(own_lines.mapped('balance'), [-780.0])
        self.assertIn('GOSI', own_lines.name)
        wages = move.line_ids.filtered(lambda l: l.account_id == self.wage)
        self.assertEqual(sorted(wages.mapped('balance')), [3610.0, 5388.0])

    def test_04i_basic_and_allowances_on_different_accounts(self):
        """AHMED NAZMI: 8,500 on the separate salary account, 2,750 on the camp."""
        man = self.env['hr.employee'].sudo().create({
            'name': 'Ledger Nazmi', 'x_posting_group_id': self.m_group.id,
            'x_posting_exception_ids': [(0, 0, {'category_id': self.cat_housing.id,
                                                'account_id': self.camp_wage.id})]})
        self._payslip([(self.rule_basic, 8500.0), (self.rule_hra, 2750.0),
                       (self.rule_net, 11250.0)], employee=man)
        self.assertEqual(self._lines(self._confirm()), {
            (self.saudi_wage, 8500.0), (self.camp_wage, 2750.0), (self.payable, -11250.0)})

    def test_04i2_an_exception_says_what_it_overrides(self):
        man = self.env['hr.employee'].sudo().create({
            'name': 'Ledger Nazmi Two', 'x_posting_group_id': self.m_group.id,
            'x_posting_exception_ids': [(0, 0, {'category_id': self.cat_housing.id,
                                                'account_id': self.camp_wage.id})]})
        row = man.x_posting_exception_ids
        self.assertEqual(row.group_id, self.m_group)
        self.assertEqual(row.default_account_id, self.saudi_wage)
        self.assertIn(self.saudi_wage.code, row.summary)
        self.assertIn(self.camp_wage.code, row.summary)
        # The whole picture: the wage stays on M (group), housing moves (exception).
        preview = str(man.x_posting_preview)
        self.assertIn(self.saudi_wage.code, preview)
        self.assertIn(self.camp_wage.code, preview)
        self.assertIn('Ledger M', preview)

    def test_04j_the_map_is_the_configuration(self):
        """A new account for the camp's housing: one map row, no code."""
        camp_housing = self.env['account.account'].sudo().create(
            {'code': '9924010002', 'name': 'Ledger camp housing', 'account_type': 'expense'})
        self.camp_group.write({'map_ids': [(0, 0, {'category_id': self.cat_housing.id,
                                                   'account_id': camp_housing.id})]})
        dept = self.env['hr.department'].sudo().create(
            {'name': 'Ledger Camp Two', 'x_posting_group_id': self.camp_group.id})
        man = self.env['hr.employee'].sudo().create({'name': 'Ledger Camp Two Man', 'department_id': dept.id})
        self._payslip([(self.rule_basic, 6000.0), (self.rule_hra, 2000.0),
                       (self.rule_net, 8000.0)], employee=man)
        self.assertEqual(self._lines(self._confirm()), {
            (self.camp_wage, 6000.0), (camp_housing, 2000.0), (self.payable, -8000.0)})

    def test_04k_deduction_against_the_basic_only(self):
        """Treatment 'basic': the allowance is left whole."""
        self.rule_ded.sudo().x_ledger_deduction = 'basic'
        self._payslip([(self.rule_basic, 2500.0), (self.rule_hra, 500.0),
                       (self.rule_ded, -2419.0), (self.rule_net, 581.0)])
        self.assertEqual(self._lines(self._confirm()), {
            (self.wage, 81.0), (self.housing, 500.0), (self.payable, -581.0)})

    def test_04l_an_exception_without_category_moves_the_whole_salary(self):
        man = self.env['hr.employee'].sudo().create({
            'name': 'Ledger Moved', 'x_posting_exception_ids': [(0, 0, {'account_id': self.camp_wage.id})]})
        self._payslip([(self.rule_basic, 3000.0), (self.rule_hra, 1000.0),
                       (self.rule_net, 4000.0)], employee=man)
        self.assertEqual(self._lines(self._confirm()), {
            (self.camp_wage, 4000.0), (self.payable, -4000.0)})

    def test_04m_no_row_suspense_or_held_out(self):
        """A group without a row: held out and named; with a suspense account, posted there."""
        empty = self.env['ksw.payroll.posting.group'].sudo().create({'name': 'Ledger Empty'})
        man = self.env['hr.employee'].sudo().create({'name': 'Ledger Unmapped', 'x_posting_group_id': empty.id})
        slip = self._payslip([(self.rule_basic, 1000.0), (self.rule_net, 1000.0)], employee=man)
        self.assertFalse(self._confirm())
        self.assertFalse(slip.x_ledger_move_id)
        self.assertIn('Ledger Unmapped', self.batch.x_ledger_error)
        suspense = self.env['account.account'].sudo().create(
            {'code': '9999990001', 'name': 'Ledger suspense', 'account_type': 'expense'})
        self.env['ir.config_parameter'].sudo().set_param('ksw_payroll_ledger.suspense_account_id', str(suspense.id))
        self.batch.action_ksw_ledger_post()
        self.assertEqual(self._lines(slip.x_ledger_move_id), {(suspense, 1000.0), (self.payable, -1000.0)})
        # The message is in the voucher language (Arabic): the account code reads in both.
        self.assertIn(suspense.code, self.batch.x_ledger_error)

    def test_05_one_employee_never_holds_up_the_others(self):
        self.rule_gosi.sudo().x_bas_posting = 'none'
        other = self.env['hr.employee'].sudo().create({'name': 'Ledger Fine Man'})
        bad = self._payslip([(self.rule_basic, 5000.0), (self.rule_gosi, -500.0),
                             (self.rule_net, 4500.0)])
        good = self._payslip([(self.rule_basic, 3000.0), (self.rule_net, 3000.0)], employee=other)
        move = self._confirm()
        self.assertEqual(good.x_ledger_move_id, move)
        self.assertFalse(bad.x_ledger_move_id)
        self.assertEqual(bad.state, 'done')
        self.assertIn('GOSI', self.batch.x_ledger_error)
        # Fixed, then posted on its own: the good slip is not booked twice.
        self.rule_gosi.sudo().x_bas_posting = 'liability'
        self.batch.action_ksw_ledger_post()
        self.assertTrue(bad.x_ledger_move_id)
        self.assertNotEqual(bad.x_ledger_move_id, move)
        self.assertEqual(good.x_ledger_move_id, move)
        self.assertFalse(self.batch.x_ledger_error)

    def test_06_one_paying_account_at_a_time(self):
        """Mark As Done for some bank accounts: those are booked now, the batch stays draft."""
        other = self.env['hr.employee'].sudo().create({'name': 'Ledger Second Bank'})
        first = self._payslip([(self.rule_basic, 5000.0), (self.rule_net, 5000.0)])
        second = self._payslip([(self.rule_basic, 3000.0), (self.rule_net, 3000.0)], employee=other)
        move = self._confirm(first)
        self.assertEqual(self.batch.state, 'draft')
        self.assertEqual(first.x_ledger_move_id, move)
        self.assertFalse(second.x_ledger_move_id)
        self._confirm(second)
        self.assertEqual(self.batch.state, 'done')
        self.assertTrue(second.x_ledger_move_id)
        self.assertNotEqual(second.x_ledger_move_id, move)
        self.assertEqual(len(self.batch.x_ledger_move_ids), 2)

    def test_07_reopening_the_batch_keeps_the_entries(self):
        """Its slips stay confirmed and paid, so their entries stand."""
        self._payslip([(self.rule_basic, 5000.0), (self.rule_net, 5000.0)])
        move = self._confirm()
        self.batch.write({'state': 'draft'})
        self.assertEqual(move.state, 'posted')
        self.assertEqual(self.batch.x_ledger_move_ids, move)

    def test_08_a_cancelled_payslip_is_reversed_on_its_own(self):
        other = self.env['hr.employee'].sudo().create({'name': 'Ledger Stays'})
        gone = self._payslip([(self.rule_basic, 5000.0), (self.rule_net, 5000.0)])
        stays = self._payslip([(self.rule_basic, 3000.0), (self.rule_net, 3000.0)], employee=other)
        move = self._confirm()
        gone.write({'state': 'cancel'})
        self.assertFalse(gone.x_ledger_move_id)
        self.assertEqual(stays.x_ledger_move_id, move)
        self.assertEqual(move.state, 'posted')
        reversal = self.env['account.move'].search(
            [('journal_id', '=', move.journal_id.id), ('id', '!=', move.id),
             ('ref', 'ilike', gone.name)])
        self.assertEqual(self._lines(reversal), {(self.wage, -5000.0), (self.payable, 5000.0)})

    def test_10_a_payslip_that_nets_to_nothing_is_nothing_to_post(self):
        """An Isuzu driver whose whole basic went to attendance (Oct 2026): BAS books nothing."""
        other = self.env['hr.employee'].sudo().create({'name': 'Ledger Zero Driver'})
        zero = self._payslip([(self.rule_basic, 600.0), (self.rule_gross, 600.0),
                              (self.rule_ded, -600.0), (self.rule_net, 0.0)], employee=other)
        paid = self._payslip([(self.rule_basic, 5000.0), (self.rule_net, 5000.0)])
        move = self._confirm()
        self.assertEqual(paid.x_ledger_move_id, move)
        self.assertFalse(zero.x_ledger_move_id)
        self.assertTrue(zero.x_ledger_nothing)
        self.assertEqual(self.batch.x_ledger_unposted_count, 0)
        self.assertFalse(self.batch.x_ledger_error)

    def test_09_the_bas_export_is_untouched(self):
        """The voucher file still reads the BAS mapping, not the ledger one."""
        self._payslip([(self.rule_basic, 5000.0), (self.rule_hra, 1250.0),
                       (self.rule_gross, 6250.0), (self.rule_net, 6250.0)])
        codes = {r['code'] for r in self.batch._bas_journal_rows()}
        self.assertIn('3101010002', codes)
        self.assertNotIn(self.housing.code, codes)

    def test_11_payroll_user_without_accounting_opens_the_batch(self):
        """a.mahmoud (Nov 2026): Mark As Done for the pending banks, no accounting rights.

        The entries are account.move records; the form must not read them for him."""
        user = self.env['res.users'].sudo().create({
            'name': 'Ledger Payroll Only', 'login': 'ledger_payroll_only',
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id,
                                  self.env.ref('om_hr_payroll.group_hr_payroll_manager').id])]})
        self.assertFalse(user.has_group('account.group_account_readonly'))
        self._payslip([(self.rule_basic, 5000.0), (self.rule_net, 5000.0)])
        self.assertTrue(self._confirm())
        Run = self.env['hr.payslip.run'].with_user(user)
        arch = Run.get_views([(False, 'form')])['views']['form']['arch']
        self.assertNotIn('name="x_ledger_move_ids"', arch)
        self.env.invalidate_all()  # the compute must run as him, not come from the cache
        Run.browse(self.batch.id).web_read({
            'x_ledger_move_count': {}, 'x_ledger_unposted_count': {}, 'x_ledger_error': {}})

    def test_12_lines_carry_the_employee_and_department(self):
        """Entry 93 (Nov 2026): 168 lines, and no way to pick out one man or department."""
        dept = self.env['hr.department'].sudo().create({'name': 'Ledger Filter Dept'})
        self.employee.sudo().department_id = dept
        other = self._employee('Ledger Filter Other', self.employee.sudo().x_salary_bank_account_id)
        self._payslip([(self.rule_basic, 5000.0), (self.rule_net, 5000.0)])
        self._payslip([(self.rule_basic, 3000.0), (self.rule_net, 3000.0)], other)
        move = self._confirm()
        mine = move.line_ids.filtered(lambda l: l.x_employee_id == self.employee)
        self.assertEqual({(l.account_id, round(l.balance, 2)) for l in mine},
                         {(self.wage, 5000.0), (self.payable, -5000.0)})
        self.assertEqual(mine.x_department_id, dept)
        self.assertEqual(move.line_ids.x_employee_id, self.employee | other)
        # A department transfer later does not move the booked lines.
        self.employee.sudo().department_id = False
        self.assertEqual(mine.x_department_id, dept)

        # An accountant without HR rights opens the entry's lines in the searchable list.
        user = self.env['res.users'].sudo().create({
            'name': 'Ledger Accountant Only', 'login': 'ledger_accountant_only',
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id,
                                  self.env.ref('account.group_account_user').id])]})
        self.assertFalse(user.has_group('hr.group_hr_user'))
        domain = [('move_id', '=', move.id)]
        if hasattr(move, 'action_ksw_open_journal_items'):  # KSW_accounting_ux's button
            domain = move.with_user(user).action_ksw_open_journal_items()['domain']
            self.assertIn(('move_id', '=', move.id), domain)
        self.env.invalidate_all()
        Line = self.env['account.move.line'].with_user(user)
        found = Line.search(domain + [('x_employee_id', '=', self.employee.id)])
        self.assertEqual(found, mine)
        rows = found.web_read({'x_employee_id': {'fields': {'display_name': {}}},
                               'x_department_id': {'fields': {'display_name': {}}}})
        self.assertEqual(rows[0]['x_employee_id']['display_name'], self.employee.name)
        self.assertEqual(Line.search(domain + [('x_department_id', 'child_of', dept.id)]), mine)

    def test_13_label_matching_prefers_the_longest_name(self):
        Line = self.env['account.move.line']
        Employee = self.env['hr.employee'].sudo()
        short = Employee.create({'name': 'AHMED ALI'})
        long_ = Employee.create({'name': 'MOHAMMED AHMED ALI'})
        twin_a = Employee.create({'name': 'LEDGER TWIN'})
        twin_b = Employee.create({'name': 'LEDGER TWIN'})
        match = Line._ksw_employee_by_label(short | long_ | twin_a | twin_b)
        self.assertEqual(match('Salary MOHAMMED AHMED ALI October'), long_)
        self.assertEqual(match('Salary  AHMED ALI October'), short)
        self.assertFalse(match('Salary LEDGER TWIN October'))
        self.assertFalse(match('Salary AHMED ALIYU October'))

    def test_14_collection_commission_allowance_on_its_own_account(self):
        """FAISAL: 2,500 basic, 500 housing, 250 mobile, 1,000 collection commission.

        BAS (July 2026, 26007185): mobile on its own main-office account, the
        collection commission on 3203020006 whatever the branch."""
        rule = self.env.ref('KSW_payroll.hr_rule_collection_commission').sudo()
        self.assertEqual(rule.code, 'COLLECTION_COMM')
        self.assertEqual(rule.x_bas_posting, 'expense')
        Account = self.env['account.account'].sudo()
        mobile_acc = Account.create({'code': '9919010004', 'name': 'Ledger mobile', 'account_type': 'expense'})
        collection_acc = Account.create(
            {'code': '9923020006', 'name': 'Ledger collection commission', 'account_type': 'expense'})
        cat_mobile = self.env['ksw.payroll.posting.category'].sudo().create(
            {'name': 'Ledger Mobile', 'code': 'LEDGER_MOBILE'})
        rule_mobile = self._rule('Mobile', 'om_hr_payroll.ALW', 13)
        rule_mobile.write({'x_bas_posting': 'expense', 'x_posting_category_id': cat_mobile.id,
                           'x_ledger_account_id': False})
        self.main_group.write({'map_ids': [(0, 0, {'category_id': cat_mobile.id, 'account_id': mobile_acc.id})]})
        rule.write({'x_ledger_account_id': collection_acc.id, 'x_posting_category_id': False})
        self._payslip([(self.rule_basic, 2500.0), (self.rule_hra, 500.0), (rule_mobile, 250.0),
                       (rule, 1000.0), (self.rule_gross, 4250.0), (self.rule_net, 4250.0)])
        self.assertEqual(self._lines(self._confirm()), {
            (self.wage, 2500.0), (self.housing, 500.0), (mobile_acc, 250.0),
            (collection_acc, 1000.0), (self.payable, -4250.0)})

    def test_15_one_catch_all_row_per_group_and_employee(self):
        """unique() lets NULLs repeat: a second wage row would make the wage account arbitrary."""
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'), self.cr.savepoint():
            self.main_group.write({'map_ids': [(0, 0, {'account_id': self.camp_wage.id})]})
            self.env.flush_all()
        man = self.env['hr.employee'].sudo().create(
            {'name': 'Ledger Two Moves', 'x_posting_exception_ids': [(0, 0, {'account_id': self.camp_wage.id})]})
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'), self.cr.savepoint():
            man.write({'x_posting_exception_ids': [(0, 0, {'account_id': self.saudi_wage.id})]})
            self.env.flush_all()
