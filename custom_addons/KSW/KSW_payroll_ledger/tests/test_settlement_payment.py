"""A leave settlement in the ledger, and paying it.

Modelled on BAS «تصفيات» 26011024 (SULEMAN UMMED KHAN, 10 Nov 2026): every
figure on its own line and account, the net to salaries payable, dated GM
final approval. Paying it is the accountant's choice on the request: a bank
file made on the spot, a cheque (paid when handed over, out of the bank when
cashed), or cash the disbursement officer hands over. Each is one entry
clearing the payable; nothing about it can block the approval.
"""
from datetime import date, datetime
from unittest.mock import patch

from odoo.exceptions import UserError

from .test_payroll_ledger import PayrollLedgerCommon

ACC_GROUP = 'KSW_annual_leave.group_annual_leave_acc'
DISB_GROUP = 'KSW_deduction.group_loan_disbursement'


def _no_export(self, run, mode):
    """The exporter's output, as it leaves it: an attachment re-homed on the file."""
    return self.env['ir.attachment'].sudo().create({
        'name': '%s.%s' % (self.name.replace('/', '-'), 'txt' if mode == 'all_txt' else 'xlsx'),
        'raw': b'stub', 'res_model': self._name, 'res_id': self.id})


class TestSettlementPayment(PayrollLedgerCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        Account = env['account.account'].sudo()

        def acc(code, kind='expense'):
            return Account.search([('code', '=', code)], limit=1) or Account.create(
                {'code': code, 'name': 'Settlement test %s' % code, 'account_type': kind})
        cls.vacation = acc('9922020001')
        cls.ticket = acc('9922020002')
        cls.penalty_fund = acc('9904010004', 'liability_current')
        cls.own = acc('9905010339', 'asset_current')
        cls.rajhi = acc('9902010005', 'asset_cash')
        cls.arab_overdraft = acc('9903010001', 'liability_current')
        cls.arab_gl = acc('9902010007', 'asset_cash')
        cls.box = acc('9901020008', 'asset_cash')
        Journal = env['account.journal'].sudo()
        cls.rajhi_journal = Journal.create({'name': 'Test Rajhi', 'code': 'TRJ', 'type': 'bank',
                                            'default_account_id': cls.rajhi.id})
        cls.arab_journal = Journal.create({'name': 'Test Arab', 'code': 'TAR', 'type': 'bank',
                                           'default_account_id': cls.arab_gl.id,
                                           'x_ledger_account_id': cls.arab_overdraft.id})
        cls.box_journal = Journal.create({'name': 'Test Box', 'code': 'TBX', 'type': 'cash',
                                          'default_account_id': cls.box.id})
        cls.kawther_bank.x_ledger_journal_id = cls.rajhi_journal

        Category = env['ksw.payroll.posting.category'].sudo()
        cls.cat_vac = Category.create({'name': 'Test Vacation', 'code': 'T_VACATION'})
        cls.cat_ticket = Category.create({'name': 'Test Ticket', 'code': 'T_TICKET'})
        cls.main_group.write({'map_ids': [
            (0, 0, {'category_id': cls.cat_vac.id, 'account_id': cls.vacation.id}),
            (0, 0, {'category_id': cls.cat_ticket.id, 'account_id': cls.ticket.id})]})
        cls.rule_vac = cls._rule('VACATION_BAL', 'om_hr_payroll.ALW', 15)
        cls.rule_ticket = cls._rule('FLIGHT_TICKET', 'om_hr_payroll.ALW', 16)
        cls.rule_penalty = cls._rule('PENALTY', 'om_hr_payroll.DED', 41)
        cls.rule_excess = cls._rule('FIN_CONSIDERATION', 'om_hr_payroll.DED', 42)
        cls.rule_visa = cls._rule('VISA_COST_RECOVERY', 'om_hr_payroll.DED', 43)
        for rule, vals in (
                (cls.rule_vac, {'x_bas_posting': 'expense', 'x_posting_category_id': cls.cat_vac.id}),
                (cls.rule_ticket, {'x_bas_posting': 'expense', 'x_posting_category_id': cls.cat_ticket.id}),
                (cls.rule_penalty, {'x_bas_posting': 'liability', 'x_ledger_deduction': 'own_account'}),
                (cls.rule_excess, {'x_bas_posting': 'liability', 'x_ledger_account_id': cls.penalty_fund.id}),
                (cls.rule_visa, {'x_bas_posting': 'liability', 'x_ledger_deduction': 'own_account'})):
            rule.sudo().write(dict({'x_ledger_account_id': False, 'x_posting_category_id': False}, **vals))

        cls.driver = env['hr.employee'].sudo().create({
            'name': 'Settlement Driver', 'x_salary_bank_account_id': cls.kawther_bank.id,
            'x_loan_acc_no': '9905010339'})
        cls.driver_own = env['res.partner.bank'].sudo().create({
            'acc_number': 'SA0380000000608010167519', 'partner_id': cls.driver.work_contact_id.id})
        cls.driver.sudo().bank_account_ids = [(6, 0, cls.driver_own.ids)]
        # A second Kawther channel (WPS) and one of Al-Hayat's.
        cls.kawther_wps = env['res.partner.bank'].sudo().create({
            'acc_number': 'LEDGER Kawther WPS', 'partner_id': env.company.partner_id.id,
            'x_ledger_entity_id': cls.kawther.id, 'x_ledger_journal_id': cls.rajhi_journal.id})
        cls.hayat_bank.x_ledger_journal_id = cls.rajhi_journal

        Users = env['res.users'].sudo().with_context(no_reset_password=True)

        def user(login, groups):
            return Users.create({'name': login, 'login': login, 'email': '%s@example.com' % login,
                                 'group_ids': [(6, 0, [env.ref('base.group_user').id] + [
                                     env.ref(g).id for g in groups])]})
        cls.user_acc = user('settle_acc', [ACC_GROUP])
        cls.user_other_acc = user('settle_acc_other', [ACC_GROUP])
        cls.user_disb = user('settle_disb', [DISB_GROUP])
        acc_emp = env['hr.employee'].sudo().create({'name': 'Settle Acc', 'user_id': cls.user_acc.id})
        # The driver has no department: his accounting step is the company team's.
        env.company.sudo().x_default_accountant_ids = [(6, 0, acc_emp.ids)]
        cls.leave_type = env['hr.leave.type'].sudo().create({
            'name': 'Settlement Test Vacation', 'requires_allocation': False,
            'leave_validation_type': 'no_validation'})

    # ------------------------------------------------------------------ helpers
    def _settlement(self, lines=None, day=9):
        """SULEMAN's settlement, confirmed as GM final approval confirms it."""
        leave = self.env['hr.leave'].sudo().create({
            'employee_id': self.driver.id, 'holiday_status_id': self.leave_type.id,
            'request_date_from': date(2026, 11, day), 'request_date_to': date(2026, 11, day + 1)})
        leave.x_gm_final_approved_date = datetime(2026, 11, 9, 6, 0)   # 09:00 Riyadh
        slip = self.env['hr.payslip'].sudo().create({
            'name': 'Settlement', 'employee_id': self.driver.id,
            'date_from': date(2026, 10, 1), 'date_to': date(2026, 10, 31),
            'version_id': self.driver.current_version_id.id, 'x_leave_id': leave.id})
        lines = lines or [
            (self.rule_basic, 1500.0), (self.rule_vac, 2189.0), (self.rule_ticket, 2620.0),
            (self.rule_gross, 6309.0), (self.rule_penalty, -714.0), (self.rule_excess, -774.0),
            (self.rule_visa, -100.0), (self.rule_net, 4721.0)]
        for rule, amount in lines:
            self.env['hr.payslip.line'].sudo().create({
                'slip_id': slip.id, 'salary_rule_id': rule.id, 'code': rule.code, 'name': rule.name,
                'category_id': rule.category_id.id, 'employee_id': self.driver.id,
                'version_id': self.driver.current_version_id.id,
                'amount': amount, 'quantity': 1, 'rate': 100, 'sequence': rule.sequence})
        slip.write({'state': 'done'})
        return leave, slip

    def _wizard(self, leave, user=None, **vals):
        if vals.get('method', 'bank') == 'bank' and 'pay_from_id' not in vals:
            vals['pay_from_id'] = self.kawther_bank.id
        return self.env['ksw.settlement.payment.wizard'].with_user(user or self.user_acc).create(
            dict({'leave_id': leave.id, 'mode': 'pay'}, **vals))

    def _pay(self, leave, user=None, **vals):
        wizard = self._wizard(leave, user, **vals)
        with patch.object(type(self.env['ksw.revision.bank.file']), '_export', _no_export):
            wizard.action_confirm()
        leave.invalidate_recordset()

    def _pending(self, leave, user):
        Leave = self.env['hr.leave'].with_user(user)
        return leave in Leave.sudo().with_user(user).search(
            [('id', '=', leave.id), ('x_is_pending_my_action', '=', True)])

    # ------------------------------------------------------------------ booking
    def test_01_booked_at_gm_final_approval_as_bas(self):
        leave, slip = self._settlement()
        move = slip.x_ledger_move_id
        self.assertTrue(move and slip.x_ledger_settlement)
        self.assertEqual(move.journal_id.code, 'SAL')
        # Its approval day, not the payslip's period end (31 Oct).
        self.assertEqual(move.date, date(2026, 11, 9))
        self.assertEqual(self._lines(move), {
            (self.wage, 1500.0), (self.vacation, 2189.0), (self.ticket, 2620.0),
            (self.own, -714.0), (self.own, -100.0), (self.penalty_fund, -774.0),
            (self.payable, -4721.0)})
        # Each figure says what it is.
        self.assertEqual(len(move.line_ids), 7)
        self.assertEqual(leave.x_disb_state, 'to_choose')

    def test_02_cancelled_before_payment_is_reversed(self):
        leave, slip = self._settlement()
        booked = slip.x_ledger_move_id
        slip.write({'state': 'cancel'})
        reversal = self.env['account.move'].sudo().search(
            [('ref', 'like', 'Reversal of %s' % slip.name)])
        self.assertEqual(len(reversal), 1)
        self.assertEqual(sum(reversal.line_ids.filtered(lambda l: l.account_id == self.payable).mapped('balance')),
                         4721.0)
        self.assertFalse(slip.x_ledger_move_id)
        self.assertTrue(booked)
        self.assertEqual(leave.x_disb_state, 'none')

    def test_03_a_problem_never_blocks_the_approval(self):
        self.env['ir.config_parameter'].sudo().set_param('ksw_payroll_ledger.suspense_account_id', False)
        self.rule_ticket.sudo().x_posting_category_id = False
        self.rule_ticket.sudo().x_bas_posting = 'none'      # nobody says where it goes
        leave, slip = self._settlement()
        self.assertEqual(slip.state, 'done')
        self.assertFalse(slip.x_ledger_move_id)
        self.assertTrue(slip.x_ledger_error)
        self.assertTrue(leave.x_disb_error)
        self.rule_ticket.sudo().write({'x_bas_posting': 'expense', 'x_posting_category_id': self.cat_ticket.id})
        leave.with_user(self.user_acc).action_disb_post_ledger()
        self.assertTrue(slip.x_ledger_move_id)
        self.assertFalse(slip.x_ledger_error)

    # ------------------------------------------------------------------ waiting for you
    def test_04_waiting_for_the_requests_accountant_only(self):
        leave, _slip = self._settlement()
        self.assertTrue(self._pending(leave, self.user_acc))
        self.assertFalse(self._pending(leave, self.user_other_acc))
        self.assertFalse(self._pending(leave, self.user_disb))
        with self.assertRaises(UserError):
            leave.with_user(self.user_other_acc).action_disb_pay()
        self._pay(leave, method='bank')
        self.assertFalse(self._pending(leave, self.user_acc))

    # ------------------------------------------------------------------ bank
    def test_05_bank_transfer_makes_the_file_and_clears_the_payable(self):
        leave, slip = self._settlement()
        self._pay(leave, method='bank', date=date(2026, 11, 10))
        self.assertTrue(slip.x_offcycle_bank_file_id)
        self.assertEqual(slip.x_offcycle_bank_file_id.state, 'done')
        self.assertEqual(leave.x_disb_state, 'paid')
        self.assertEqual(leave.x_disb_bank_file_id, slip.x_offcycle_bank_file_id)
        pay = slip.x_disb_move_id
        self.assertEqual(pay.journal_id, self.rajhi_journal)
        self.assertEqual(pay.date, date(2026, 11, 10))
        self.assertEqual(self._lines(pay), {(self.payable, 4721.0), (self.rajhi, -4721.0)})
        # The settlement entry is not booked again with the hidden batch.
        self.assertEqual(self.env['account.move'].sudo().search_count(
            [('line_ids.x_employee_id', '=', self.driver.id), ('journal_id.code', '=', 'SAL')]), 1)

    def test_06_a_paying_account_without_journal_is_refused_before_the_file(self):
        self.kawther_bank.x_ledger_journal_id = False
        leave, slip = self._settlement()
        with self.assertRaises(UserError):
            self._pay(leave, method='bank')
        self.assertFalse(slip.x_offcycle_bank_file_id)
        self.assertEqual(leave.x_disb_state, 'to_choose')
        # Fixed in Accounting, paid, posted.
        self.kawther_bank.x_ledger_journal_id = self.rajhi_journal
        self._pay(leave, method='bank')
        self.assertTrue(slip.x_disb_move_id)
        self.assertFalse(slip.x_disb_error)

    def test_07_the_menu_file_also_posts_the_transfer(self):
        leave, slip = self._settlement()
        File = self.env['ksw.revision.bank.file'].with_user(self.user_acc)
        bank_file = File.create({'leave_line_ids': [(0, 0, {'payslip_id': slip.id})]})
        with patch.object(type(self.env['ksw.revision.bank.file']), '_export', _no_export):
            bank_file.action_generate()
        self.assertEqual(slip.x_disb_method, 'bank')
        self.assertEqual(self._lines(slip.x_disb_move_id), {(self.payable, 4721.0), (self.rajhi, -4721.0)})

    # ------------------------------------------------------------------ cheque
    def test_08_cheque_is_paid_when_handed_and_leaves_the_bank_when_cashed(self):
        leave, slip = self._settlement()
        self._pay(leave, method='cheque', journal_id=self.arab_journal.id, cheque_no='54423',
                  date=date(2026, 11, 11))
        self.assertEqual(leave.x_disb_state, 'cheque_handed')
        cheques = self.arab_journal.x_cheques_account_id
        self.assertTrue(cheques.code.startswith('211301'))
        self.assertEqual(self._lines(slip.x_disb_move_id), {(self.payable, 4721.0), (cheques, -4721.0)})
        # Held by the employee: no bank file may pay it, no reversal release it.
        self.assertNotIn(slip, self.env['hr.payslip']._ksw_offcycle_pending_leave_slips())
        with self.assertRaises(UserError):
            slip.with_user(self.user_acc).sudo().write({'state': 'cancel'})
        self.assertFalse(self._pending(leave, self.user_acc))
        self.assertIn(leave, self.env['hr.leave'].sudo().search([('x_disb_state', '=', 'cheque_handed')]))

        with self.assertRaises(UserError):
            leave.with_user(self.user_other_acc).action_disb_cheque_cashed()
        wizard = self.env['ksw.settlement.payment.wizard'].with_user(self.user_acc).create(
            {'leave_id': leave.id, 'mode': 'cashed', 'date': date(2026, 11, 20)})
        wizard.action_confirm()
        leave.invalidate_recordset()
        self.assertEqual(leave.x_disb_state, 'paid')
        # Out of Arab Bank's overdraft account, as BAS pays its cheques.
        self.assertEqual(self._lines(slip.x_disb_cashed_move_id),
                         {(cheques, 4721.0), (self.arab_overdraft, -4721.0)})
        self.assertEqual(round(sum(self.env['account.move.line'].sudo().search(
            [('account_id', '=', cheques.id), ('parent_state', '=', 'posted')]).mapped('balance')), 2), 0.0)

    def test_09_cheque_needs_its_number_and_bank(self):
        leave, _slip = self._settlement()
        with self.assertRaises(UserError):
            self._pay(leave, method='cheque', journal_id=self.arab_journal.id, cheque_no=' ')
        with self.assertRaises(UserError):
            self._pay(leave, method='cheque', journal_id=self.box_journal.id, cheque_no='1')
        self.assertEqual(leave.x_disb_state, 'to_choose')

    # ------------------------------------------------------------------ cash
    def test_10_cash_waits_for_the_disbursement_officer(self):
        leave, slip = self._settlement()
        self._pay(leave, method='cash', journal_id=self.box_journal.id)
        self.assertEqual(leave.x_disb_state, 'cash_pending')
        self.assertFalse(slip.x_disb_move_id)
        self.assertTrue(self._pending(leave, self.user_disb))
        self.assertFalse(self._pending(leave, self.user_acc))
        self.assertNotIn(slip, self.env['hr.payslip']._ksw_offcycle_pending_leave_slips())
        with self.assertRaises(UserError):
            leave.with_user(self.user_acc).action_disb_cash_handed()
        # He can open what waits on him, and only that.
        self.assertTrue(self.env['hr.leave'].with_user(self.user_disb).search([('id', '=', leave.id)]))
        leave.with_user(self.user_disb).action_disb_cash_handed()
        leave.invalidate_recordset()
        self.assertEqual(leave.x_disb_state, 'paid')
        self.assertFalse(self._pending(leave, self.user_disb))
        self.assertEqual(self._lines(slip.x_disb_move_id), {(self.payable, 4721.0), (self.box, -4721.0)})
        self.assertTrue(slip.x_paid_outside_bank_file)

    # ------------------------------------------------------------------ the old ones
    def test_11_rebook_a_settlement_booked_with_its_batch(self):
        leave, slip = self._settlement()
        Ledger = self.env['ksw.payroll.ledger']
        # As entry 135: in the off-cycle batch, one wage line, dated period end.
        Ledger._cancel(slip.x_ledger_move_id)
        old = Ledger._move(Ledger._journal('SAL', self.env.company), date(2026, 10, 31), 'old',
                           [(self.wage, 4721.0, 'Salary', None), (self.payable, -4721.0, 'Salary', None)])
        slip.write({'x_ledger_move_id': old.id, 'x_ledger_settlement': False})
        dry = Ledger._rebook_settlements(dry_run=True)
        self.assertEqual(len(dry['rebooked']), 1)
        self.assertEqual(old.state, 'posted')
        Ledger._rebook_settlements(dry_run=False)
        self.assertEqual(old.state, 'cancel')
        self.assertTrue(slip.x_ledger_settlement)
        self.assertEqual(slip.x_ledger_move_id.date, date(2026, 11, 9))
        self.assertIn((self.vacation, 2189.0), self._lines(slip.x_ledger_move_id))
        self.assertFalse(Ledger._rebook_settlements(dry_run=True)['rebooked'])

    def test_12_a_settlement_books_what_was_paid_to_the_riyal(self):
        """SLIP/14525: its rounded lines come to 4,777, its NET (and the
        transfer) 4,776. Up to one riyal goes on the largest expense; more is
        a real difference and is refused."""
        leave, slip = self._settlement([
            (self.rule_basic, 2000.0), (self.rule_vac, 1757.0), (self.rule_gross, 3757.0),
            (self.rule_net, 3756.0)])
        self.assertEqual(self._lines(slip.x_ledger_move_id), {
            (self.wage, 1999.0), (self.vacation, 1757.0), (self.payable, -3756.0)})
        _leave, far = self._settlement([
            (self.rule_basic, 2000.0), (self.rule_gross, 2000.0), (self.rule_net, 1990.0)], day=16)
        self.assertFalse(far.x_ledger_move_id)
        self.assertTrue(far.x_ledger_error)

    def test_13_labels_in_the_vouchers_language(self):
        lang = self.env['ksw.bas.journal'].voucher_lang()
        if not lang:
            self.skipTest('no voucher language installed')
        self.rule_vac.sudo().with_context(lang=lang).name = 'تسوية رصيد الإجازة'
        _leave, slip = self._settlement()
        labels = slip.x_ledger_move_id.line_ids.mapped('name')
        self.assertTrue(any(l.startswith('تسوية رصيد الإجازة') for l in labels), labels)

    def _open_form(self, leave, user):
        """What the browser does to open the request: the form's own fields,
        read as that user (pitfalls #31/#32: read() hides these crashes)."""
        Leave = self.env['hr.leave'].with_user(user)
        fields_ = Leave.get_views([(False, 'form')])['models']['hr.leave']['fields']
        self.env.invalidate_all()
        return Leave.browse(leave.id).web_read({name: {} for name in fields_})

    def test_14_the_officer_and_the_accountant_can_open_the_request(self):
        leave, _slip = self._settlement()
        self.assertTrue(self._open_form(leave, self.user_acc)[0]['x_can_disb_pay'])
        self._pay(leave, method='cash', journal_id=self.box_journal.id)
        row = self._open_form(leave, self.user_disb)[0]
        self.assertTrue(row['x_can_disb_cash'])
        self.assertEqual(row['x_disb_amount'], 4721.0)
        self.assertEqual(row['x_disb_journal_name'], self.box_journal.display_name)

    # ------------------------------------------------------------------ which accounts
    def test_15_bank_transfer_shows_from_ledger_and_beneficiary(self):
        """Three different accounts: the company channel it leaves from, the
        ledger account that channel credits, the employee's own account."""
        leave, _slip = self._settlement()
        wizard = self._wizard(leave, method='bank')
        self.assertEqual(wizard.pay_from_id, self.kawther_bank)
        self.assertIn('9902010005', wizard.pay_from_ledger)
        self.assertIn('SA0380000000608010167519', wizard.beneficiary)
        # Kawther's channels only: Hayat's would pay a payable this was not booked to.
        self.assertIn(self.kawther_wps, wizard.pay_from_ids)
        self.assertNotIn(self.hayat_bank, wizard.pay_from_ids)
        self.assertGreaterEqual(wizard.pay_from_count, 2)

    def test_16_another_channel_of_the_same_entity_is_stamped(self):
        leave, slip = self._settlement()
        self._pay(leave, method='bank', pay_from_id=self.kawther_wps.id)
        self.assertEqual(slip.x_paid_bank_account_id, self.kawther_wps)
        self.assertEqual(slip.x_disb_state, 'paid')

    def test_17_no_transfer_to_nowhere_nor_from_another_entity(self):
        leave, slip = self._settlement()
        with self.assertRaises(UserError):
            self._pay(leave, method='bank', pay_from_id=self.hayat_bank.id)
        self.driver.sudo().bank_account_ids = [(5, 0, 0)]
        with self.assertRaises(UserError):
            self._pay(leave, method='bank')
        self.assertEqual(slip.x_disb_state, 'to_choose')
        self.assertFalse(slip.x_offcycle_bank_file_id)

    # ------------------------------------------------------------------ any other bank
    def test_18_direct_transfer_from_any_bank(self):
        """Riyad, Arab, Al-Ahli…: no payroll file format, transferred from that
        bank's online banking. Paid at once, out of that bank's ledger account."""
        leave, slip = self._settlement()
        self._pay(leave, method='transfer', journal_id=self.arab_journal.id, reference='FT26112601',
                  date=date(2026, 11, 12))
        self.assertEqual(leave.x_disb_state, 'paid')
        self.assertFalse(slip.x_offcycle_bank_file_id)
        self.assertEqual(slip.x_disb_reference, 'FT26112601')
        self.assertEqual(slip.x_disb_move_id.journal_id, self.arab_journal)
        self.assertEqual(slip.x_disb_move_id.date, date(2026, 11, 12))
        self.assertEqual(self._lines(slip.x_disb_move_id),
                         {(self.payable, 4721.0), (self.arab_overdraft, -4721.0)})
        # Sent: no payroll file may pay it again, no reversal release it.
        self.assertNotIn(slip, self.env['hr.payslip']._ksw_offcycle_pending_leave_slips())
        with self.assertRaises(UserError):
            slip.with_user(self.user_acc).sudo().write({'state': 'cancel'})
        self.assertIn('FT26112601', leave.x_settlement_transfer_status)

    def test_19_direct_transfer_needs_a_bank_a_reference_and_a_beneficiary(self):
        leave, slip = self._settlement()
        with self.assertRaises(UserError):
            self._pay(leave, method='transfer', journal_id=self.arab_journal.id, reference=' ')
        with self.assertRaises(UserError):
            self._pay(leave, method='transfer', journal_id=self.box_journal.id, reference='X1')
        self.driver.sudo().bank_account_ids = [(5, 0, 0)]
        with self.assertRaises(UserError):
            self._pay(leave, method='transfer', journal_id=self.arab_journal.id, reference='X1')
        self.assertEqual(slip.x_disb_state, 'to_choose')

    def test_20_the_request_names_the_bank_file_and_offers_its_files(self):
        leave, slip = self._settlement()
        self._pay(leave, method='bank')
        bank_file = slip.x_offcycle_bank_file_id
        # The accountant: the reference, both files, and the file itself.
        row = self._open_form(leave, self.user_acc)[0]
        self.assertEqual(row['x_disb_bank_file_ref'], bank_file.name)
        self.assertEqual(len(row['x_disb_bank_file_attachment_ids']), 2)
        action = leave.with_user(self.user_acc).action_open_disb_bank_file()
        self.assertEqual((action['res_model'], action['res_id']), (bank_file._name, bank_file.id))
        # HR sees which file paid him, not the file (his account number).
        user_hr = self.env['res.users'].sudo().with_context(no_reset_password=True).create({
            'name': 'settle_hr', 'login': 'settle_hr', 'email': 'settle_hr@example.com',
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id,
                                  self.env.ref('KSW_annual_leave.group_annual_leave_hr').id])]})
        row = self._open_form(leave, user_hr)[0]
        self.assertEqual(row['x_disb_bank_file_ref'], bank_file.name)

        def arch(user):
            return self.env['hr.leave'].with_user(user).get_views([(False, 'form')])['views']['form']['arch']
        # The client reads what the arch shows: the files and the button only for accounting.
        self.assertIn('name="x_disb_bank_file_attachment_ids"', arch(self.user_acc))
        self.assertIn('action_open_disb_bank_file', arch(self.user_acc))
        self.assertNotIn('name="x_disb_bank_file_attachment_ids"', arch(user_hr))
        self.assertNotIn('action_open_disb_bank_file', arch(user_hr))
        with self.assertRaises(Exception):
            leave.with_user(user_hr).action_open_disb_bank_file()
