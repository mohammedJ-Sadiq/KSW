"""A commission month in BAS: every file posts the difference.

The right is proved first (Export ▸ Journal entries), deductions are taken
from it while it waits, the transfer settles it (Mark Paid). Each BAS file
is a voucher of keyed items; the next one — another export, a settlement
voucher, Mark Paid's final settlement — posts what the month says now minus
everything already posted. Earlier files stay as the month's history.
"""
from dateutil.relativedelta import relativedelta

from unittest.mock import patch

from odoo.exceptions import UserError

from .test_commission_auto_recovery import AutoRecoveryCommon

BANK_CODE = '1202010005'


class SettlementCommon(AutoRecoveryCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bank = cls.env['res.partner.bank'].create({
            'acc_number': 'CST-KAWTHAR-WPS',
            'partner_id': cls.env.company.partner_id.id,
            'x_bas_account_code': BANK_CODE,
            'x_bas_account_name': 'مصرف الراجحي',
        })
        cls.emp_a.sudo().x_salary_bank_account_id = cls.bank
        cls.env.ref('base.user_admin').sudo().write({'group_ids': [(4, cls.env.ref(
            'KSW_commissions.group_commission_accountant').id)]})
        # Whatever the database already holds is not this test's business:
        # record it as already in BAS so every voucher below starts clean.
        Settlement = cls.env['ksw.pay.settlement']
        baseline = Settlement.create({})
        if baseline.item_ids:
            baseline.action_mark_posted()
        else:
            baseline.unlink()

    # ------------------------------------------------------------------
    def _voucher(self):
        return self.env['ksw.pay.settlement'].create(
            {'journal_number': '26009001'})

    def _late_penalty(self, run, amount, days=10):
        ded = self._penalty(self.emp_a, amount)
        ded.sudo().x_charge_date = run._bas_journal_date() + relativedelta(
            days=days)
        return ded

    def _pay(self, run, number='26009100'):
        """Mark Paid as the accountant (the group check exempts no one)
        and return the voucher it posted."""
        run = run.with_user(self.env.ref('base.user_admin'))
        return run._mark_lines_paid(run.line_ids, journal_number=number)

    def _unsettled(self, run):
        run.invalidate_recordset(['x_bas_unsettled_count'])
        return run.with_user(
            self.env.ref('base.user_admin')).x_bas_unsettled_count

    def _export(self, run, number='26009001', date=None):
        return run.with_user(self.env.ref('base.user_admin'))._bas_export_month(
            journal_number=number, date=date)

    def _codes(self, rows):
        return [(r['code'], r['debit'], r['credit']) for r in rows]





class TestMonthLedger(SettlementCommon):
    """Export ▸ Journal entries posts the month once, then only changes."""

    def test_01_the_first_export_posts_the_whole_month(self):
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        voucher = self._export(run)
        self.assertEqual(voucher.origin, 'month')
        self.assertEqual(voucher.run_id, run)
        self.assertTrue(voucher.attachment_id)
        self.assertAlmostEqual(voucher.total_right, 1200.0)
        self.assertEqual(self._codes(voucher._bas_rows()), [
            ('3201010006', 1200.0, 0.0),
            ('2107010001', 0.0, 1200.0),
        ])

    def test_02_a_deduction_on_the_month_end_is_in_the_first_export(self):
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        self._late_penalty(run, 500.0, days=0)
        rows = self._export(run)._bas_rows()
        self.assertEqual(self._codes(rows), [
            ('3201010006', 1200.0, 0.0),
            ('2107010001', 0.0, 1200.0),
            ('2107010001', 500.0, 0.0),
            ('1205010385', 0.0, 500.0),
        ])
        # The same figures the untracked month voucher always produced.
        self.assertEqual(sorted(self._codes(rows)),
                         sorted(self._codes(run._bas_journal_rows())))

    def test_03_exporting_again_with_nothing_new_is_refused(self):
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        self._export(run)
        with self.assertRaisesRegex(UserError, 'Nothing changed'):
            self._export(run, number='26009002')

    def test_04_a_later_deduction_is_all_the_next_export_carries(self):
        run = self._approve(self._commission_entry(self.emp_a, 2456.0).batch_id)
        self._export(run)
        ded = self._late_penalty(run, 1000.0)
        rows = self._export(run, number='26009002')._bas_rows()
        self.assertEqual(self._codes(rows), [
            ('2107010001', 1000.0, 0.0),
            ('1205010385', 0.0, 1000.0),
        ])
        self.assertIn(ded.name, rows[0]['ref'])

    def test_04b_his_deductions_are_one_line_on_his_advance_account(self):
        """BAS has one advance account per man: add his deductions up."""
        run = self._approve(self._commission_entry(self.emp_a, 2456.0).batch_id)
        self._export(run)
        first = self._late_penalty(run, 300.0)
        second = self._late_penalty(run, 200.0)
        rows = self._export(run, number='26009002')._bas_rows()
        self.assertEqual(self._codes(rows), [
            ('2107010001', 500.0, 0.0),
            ('1205010385', 0.0, 500.0),
        ])
        self.assertIn(first.name, rows[1]['ref'])
        self.assertIn(second.name, rows[1]['ref'])

    def test_04c_a_file_can_be_rebuilt_without_touching_the_record(self):
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        voucher = self._export(run).with_user(self.env.ref('base.user_admin'))
        before = voucher.item_ids.read(['key', 'amount', 'accrual_split'])
        voucher.action_rebuild_file()
        self.assertEqual(voucher.item_ids.read(['key', 'amount', 'accrual_split']),
                         before)
        self.assertEqual(voucher.state, 'done')

    def test_05_a_changed_right_posts_only_the_difference(self):
        """BAS was told 1,200; the month now says 1,500."""
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        first = self._export(run)
        # As if the month were corrected after its export: the item BAS
        # holds is what was posted, whatever the month says now.
        first.item_ids.sudo().write({'amount': 900.0})
        rows = self._export(run, number='26009002')._bas_rows()
        self.assertEqual(self._codes(rows), [
            ('3201010006', 300.0, 0.0),
            ('2107010001', 0.0, 300.0),
        ])

    def test_06_a_right_that_went_down_is_reversed(self):
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        first = self._export(run)
        first.item_ids.sudo().write({'amount': 1500.0})
        voucher = self._export(run, number='26009002')
        self.assertAlmostEqual(voucher.total_right, -300.0)
        self.assertEqual(self._codes(voucher._bas_rows()), [
            ('3201010006', 0.0, 300.0),
            ('2107010001', 300.0, 0.0),
        ])

    def test_07_a_reopened_month_is_frozen(self):
        run = self._approve(self._commission_entry(self.emp_a, 2456.0).batch_id)
        self._late_penalty(run, 1000.0)
        self._export(run)
        run.sudo().action_reopen()
        self.assertFalse(self._voucher().item_ids,
                         'nothing is reversed while the month is corrected')

    def test_08_the_month_keeps_every_file_as_history(self):
        run = self._approve(self._commission_entry(self.emp_a, 2456.0).batch_id)
        first = self._export(run)
        self._late_penalty(run, 1000.0)
        second = self._export(run, number='26009002')
        self.assertEqual(run._bas_vouchers(), second | first)
        run.invalidate_recordset(['x_settlement_count'])
        self.assertEqual(run.x_settlement_count, 2)

    def test_09_the_export_dates_the_first_file_at_the_month_end(self):
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        Wizard = self.env['ksw.commission.bank.export.wizard']
        before = Wizard.create({'run_id': run.id, 'export_mode': 'journal_entry'})
        self.assertEqual(before.journal_date, run._bas_journal_date())
        self.assertFalse(before.journal_history)
        voucher = self._export(run)
        after = Wizard.create({'run_id': run.id, 'export_mode': 'journal_entry'})
        self.assertNotEqual(after.journal_date, run._bas_journal_date())
        self.assertIn(voucher.name, after.journal_history)


class TestMarkPaidIsTheFinalSettlement(SettlementCommon):

    def test_01_never_exported_mark_paid_posts_right_and_transfer(self):
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        voucher = self._pay(run)
        self.assertEqual(voucher.origin, 'mark_paid')
        self.assertEqual(voucher.journal_number, '26009100')
        self.assertTrue(voucher.attachment_id)
        self.assertEqual(self._codes(voucher._bas_rows()), [
            ('3201010006', 1200.0, 0.0),
            ('2107010001', 0.0, 1200.0),
            ('2107010001', 1200.0, 0.0),
            (BANK_CODE, 0.0, 1200.0),
        ])
        self.assertEqual(self._unsettled(run), 0)

    def test_02_after_an_export_only_what_is_new_and_the_transfer(self):
        run = self._approve(self._commission_entry(self.emp_a, 2456.0).batch_id)
        self._export(run)
        ded = self._late_penalty(run, 1000.0)
        rows = self._pay(run)._bas_rows()
        self.assertEqual(self._codes(rows), [
            ('2107010001', 1000.0, 0.0),
            ('1205010385', 0.0, 1000.0),
            ('2107010001', 1456.0, 0.0),
            (BANK_CODE, 0.0, 1456.0),
        ])
        self.assertIn(ded.name, rows[0]['ref'])

    def test_03_a_deduction_held_by_a_draft_stays_with_the_draft(self):
        run = self._approve(self._commission_entry(self.emp_a, 2456.0).batch_id)
        self._export(run)
        self._late_penalty(run, 1000.0)
        draft = self._voucher()
        voucher = self._pay(run)
        self.assertEqual(voucher.item_ids.kind, 'payment')
        self.assertEqual(draft.item_ids.kind, 'deduction')

    def test_04_no_bas_bank_account_means_nothing_is_marked_paid(self):
        self.bank.x_bas_account_code = False
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        # A request rolls back on the error; the savepoint is that request.
        with self.assertRaisesRegex(UserError, 'CST-KAWTHAR-WPS'):
            with self.env.cr.savepoint():
                self._pay(run)
        self.env.invalidate_all()
        self.assertFalse(run.line_ids.x_paid)
        self.assertEqual(run.state, 'approved')

    def test_05_the_mark_paid_dialog_asks_for_the_voucher(self):
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        admin = self.env.ref('base.user_admin')
        action = run.with_user(admin).action_open_mark_paid_wizard()
        wizard = self.env['ksw.pay.run.paid.wizard'].with_user(
            admin).browse(action['res_id'])
        with self.assertRaises(UserError):
            wizard.action_confirm()
        wizard.journal_number = '26009200'
        result = wizard.action_confirm()
        voucher = self.env['ksw.pay.settlement'].browse(result['res_id'])
        self.assertEqual(voucher.journal_number, '26009200')
        self.assertEqual(run.state, 'paid')

    def test_06_a_posted_voucher_is_sealed(self):
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        voucher = self._pay(run).with_user(self.env.ref('base.user_admin'))
        with self.assertRaises(UserError):
            voucher.write({'date': voucher.date + relativedelta(days=1)})
        with self.assertRaises(UserError):
            voucher.unlink()
        with self.assertRaises(UserError):
            voucher.item_ids.unlink()


class TestCommissionSettlement(SettlementCommon):
    """The accountant's cross-month voucher."""

    def test_01_it_collects_what_no_file_has_posted(self):
        run = self._approve(self._commission_entry(self.emp_a, 2456.0).batch_id)
        voucher = self._voucher()
        self.assertAlmostEqual(voucher.total_right, 2456.0,
                               'never exported: the right is pending')
        voucher.action_generate()
        self.assertEqual(self._unsettled(run), 0)
        ded = self._late_penalty(run, 1000.0)
        self.assertEqual(self._unsettled(run), 1)
        second = self._voucher()
        self.assertEqual(second.item_ids.deduction_line_id, ded.line_ids)

    def test_02_undone_after_posting_is_reversed_once_reapproved(self):
        run = self._approve(self._commission_entry(self.emp_a, 2456.0).batch_id)
        self._late_penalty(run, 1000.0)
        posted = self._export(run)
        ded_item = posted.item_ids.filtered(lambda i: i.kind == 'deduction')
        # The month is approved again without the deduction (released to
        # payroll while it was open): BAS gives it back.
        # (SQL: the deduction model's own rules are not what is under test.
        # Flush first, or pending ORM writes land on top of it.)
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE ksw_deduction_line SET state = 'pending', "
            "x_paid_via_pay_run_line_id = NULL WHERE id = %s",
            [ded_item.deduction_line_id.id])
        # Releasing a recovery gives the register its money back too.
        self.env.cr.execute(
            "UPDATE ksw_pay_run_line SET loan_offset = 0, "
            "net_payable = earnings WHERE id = %s",
            [ded_item.run_line_id.id])
        self.env.invalidate_all()
        voucher = self._voucher()
        self.assertAlmostEqual(voucher.total_deducted, -1000.0)
        self.assertEqual(self._codes(voucher._bas_rows()), [
            ('2107010001', 0.0, 1000.0),
            ('1205010385', 1000.0, 0.0),
        ])

    def test_03_a_draft_that_went_stale_is_refused(self):
        run = self._approve(self._commission_entry(self.emp_a, 2456.0).batch_id)
        voucher = self._voucher()
        run.sudo().action_reopen()
        with self.assertRaises(UserError):
            voucher.action_generate()
        voucher.action_collect()
        self.assertFalse(voucher.item_ids)

    def test_04_a_second_draft_does_not_take_the_same_lines(self):
        self._approve(self._commission_entry(self.emp_a, 2456.0).batch_id)
        first = self._voucher()
        self.assertTrue(first.item_ids)
        self.assertFalse(self._voucher().item_ids)
        first.item_ids.unlink()
        self.assertTrue(self._voucher().item_ids)

    def test_05_already_in_bas_records_without_a_file(self):
        run = self._approve(self._commission_entry(self.emp_a, 2456.0).batch_id)
        voucher = self._voucher()
        voucher.action_mark_posted()
        self.assertTrue(voucher.posted_manually)
        self.assertFalse(voucher.attachment_id)
        with self.assertRaisesRegex(UserError, 'Nothing changed'):
            self._export(run)

    def test_06_only_the_accountant(self):
        with self.assertRaises(UserError):
            self.env['ksw.pay.settlement'].with_user(self.sup_a).create({})


class TestLoadFromBas(SettlementCommon):
    """A month already in BAS becomes the starting point."""

    def _bas_rows_from(self, rows, number=26008282):
        """A BAS voucher as VOU10 returns it, from journal rows."""
        from datetime import datetime
        out = []
        for serial, row in enumerate(rows, 1):
            out.append({
                'SERIAL': serial, 'FDATE': datetime(2026, 8, 31, 9, 5),
                'FCODE': row['code'] if row['debit'] else None,
                'TCODE': row['code'] if row['credit'] else None,
                'AMOUNT': row['debit'] or row['credit'],
                'REF': ' %s' % row['ref'], 'NAME': row['name'],
            })
        return str(number), out

    def _load(self, run, rows):
        wizard = self.env['ksw.pay.bas.baseline.wizard'].with_user(
            self.env.ref('base.user_admin')).create(
                {'run_id': run.id, 'voucher_number': '26008282'})
        with patch.object(type(wizard), '_read_bas', lambda w: rows):
            wizard.action_preview()
            self.assertTrue(wizard.preview)
            action = wizard.action_load()
        return self.env['ksw.pay.settlement'].browse(action['res_id'])

    def test_01_a_matching_bas_voucher_leaves_nothing_to_post(self):
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        self._late_penalty(run, 500.0, days=0)
        loaded = self._load(run, self._bas_rows_from(run._bas_journal_rows()))
        self.assertEqual(loaded.origin, 'baseline')
        self.assertTrue(loaded.posted_manually)
        self.assertEqual(loaded.bas_voucher_number, '26008282')
        self.assertAlmostEqual(loaded.total_right, 1200.0)
        self.assertAlmostEqual(loaded.total_deducted, 500.0)
        with self.assertRaisesRegex(UserError, 'Nothing changed'):
            self._export(run)

    def test_02_bas_holding_more_than_the_month_is_corrected(self):
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        rows = run._bas_journal_rows()
        for row in rows:      # BAS was told 1,500
            row['debit'] and row.update(debit=1500.0)
            row['credit'] and row.update(credit=1500.0)
        self._load(run, self._bas_rows_from(rows))
        voucher = self._export(run)
        self.assertEqual(self._codes(voucher._bas_rows()), [
            ('3201010006', 0.0, 300.0),
            ('2107010001', 300.0, 0.0),
        ])

    def test_03_a_bas_line_for_no_one_in_the_register_is_left_alone(self):
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        rows = run._bas_journal_rows() + [
            {'code': '3201010006', 'name': 'x', 'debit': 250.0, 'credit': 0.0,
             'ref': 'بدل عمل اضافي NOBODY KNOWN شهر أغسطس', 'cost_center': ''},
            {'code': '2107010001', 'name': 'y', 'debit': 0.0, 'credit': 250.0,
             'ref': 'عمولات NOBODY KNOWN شهر أغسطس', 'cost_center': ''},
        ]
        loaded = self._load(run, self._bas_rows_from(rows))
        self.assertIn('NOBODY KNOWN', loaded.note)
        # Settled somewhere else (no one in the register): left alone.
        with self.assertRaisesRegex(UserError, 'Nothing changed'):
            self._export(run)

    def test_05_the_button_opens_the_dialog_unsaved(self):
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        action = run.with_user(
            self.env.ref('base.user_admin')).action_open_bas_baseline_wizard()
        self.assertFalse(action.get('res_id'))
        self.assertEqual(action['context']['default_run_id'], run.id)

    def test_06_a_hand_typed_block_is_placed_by_its_accounts(self):
        """KSWCO 26008282 lines 112–114: a man no longer in the month, his
        lines typed by hand, the advance credit with no name on it."""
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        rows = run._bas_journal_rows() + [
            {'code': '3203020007', 'name': 'x', 'debit': 2280.0, 'credit': 0.0,
             'ref': 'عمولات ردود %s اغسطس' % self.emp_b.name, 'cost_center': ''},
            {'code': self.emp_b.sudo().x_loan_acc_no, 'name': 'loan',
             'debit': 0.0, 'credit': 2000.0, 'ref': 'خصم من عمولات اغسطس',
             'cost_center': ''},
            {'code': '2107010001', 'name': 'y', 'debit': 0.0, 'credit': 280.0,
             'ref': 'عمولات %s اغسطس' % self.emp_b.name, 'cost_center': ''},
            # A sales-commission block: none of this month's business.
            {'code': '3203020005', 'name': 's', 'debit': 742.0, 'credit': 0.0,
             'ref': 'عمولة مبيعات اغسطس', 'cost_center': ''},
            {'code': '1205010236', 'name': 'l', 'debit': 0.0, 'credit': 300.0,
             'ref': 'خصم من عمولة اغسطس', 'cost_center': ''},
            {'code': '2107010005', 'name': 'z', 'debit': 0.0, 'credit': 442.0,
             'ref': 'عمولات مبيعات اغسطس', 'cost_center': ''},
        ]
        loaded = self._load(run, self._bas_rows_from(rows))
        theirs = loaded.item_ids.filtered(lambda i: i.employee_id == self.emp_b)
        self.assertEqual(sorted(theirs.mapped('kind')), ['accrual', 'deduction'])
        self.assertFalse(loaded.item_ids.filtered(
            lambda i: i.counter_code == '1205010236'), 'sales stays out')
        # He is not in the month's register: settled by an entry of his own
        # (KSWCO: the user, 2026-10-20). His block stays as BAS has it.
        with self.assertRaisesRegex(UserError, 'Nothing changed'):
            self._export(run)

    def test_07_the_vacation_part_of_a_pay_type_stays_in_bas(self):
        """BAS holds 1,500 for him; 1,200 is in the register and 300 was paid
        on his vacation payslip — nothing to post."""
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        first = self._export(run)
        first.item_ids.sudo().write({'amount': 1500.0})
        key = first.item_ids.key
        Settlement = type(self.env['ksw.pay.settlement'])
        with patch.object(Settlement, '_vacation_parts',
                          lambda self, runs: {key: 300.0}):
            with self.assertRaisesRegex(UserError, 'Nothing changed'):
                self._export(run, number='26009002')
        # Without the vacation part the 300 is a real decrease.
        voucher = self._export(run, number='26009003')
        self.assertAlmostEqual(voucher.total_right, -300.0)

    def test_08_a_zero_block_does_not_swallow_the_next_man(self):
        """KSWCO 26008282: a man whose loan ate his whole month has no
        accrual line, so the next man's lines run on into his block."""
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        own = run._bas_journal_rows()
        month = ' '.join(own[0]['ref'].split()[-2:])     # «شهر <month>»
        rows = [
            {'code': '3201010006', 'name': 'x', 'debit': 400.0, 'credit': 0.0,
             'ref': 'بدل عمل اضافي %s %s' % (self.emp_b.name, month),
             'cost_center': ''},
            {'code': self.emp_b.sudo().x_loan_acc_no, 'name': 'loan',
             'debit': 0.0, 'credit': 400.0,
             'ref': 'خصم من عمولات %s %s' % (self.emp_b.name, month),
             'cost_center': ''},
        ] + own
        loaded = self._load(run, self._bas_rows_from(rows))
        mine = loaded.item_ids.filtered(lambda i: i.employee_id == self.emp_a)
        self.assertAlmostEqual(sum(mine.mapped('amount')), 1200.0)
        with self.assertRaisesRegex(UserError, 'Nothing changed'):
            self._export(run)

    def test_09_the_ledger_speaks_bas_language_whoever_looks(self):
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        S = self.env['ksw.pay.settlement']
        en = set(S.with_context(lang='en_US')._desired_events(run.with_context(lang='en_US')))
        ar = set(S.with_context(lang='ar_001')._desired_events(run.with_context(lang='ar_001')))
        self.assertEqual(en, ar)

    def test_10_an_amount_the_gm_ordered_gets_its_own_line(self):
        """KSWCO August: the GM's extra 500 was typed into the Driver Trips
        line in BAS. Entered in Odoo as what it is, the next export moves it
        to its own description; the totals do not change."""
        gm = self.env.ref('KSW_commissions.pay_component_gm_extra')
        meals = self._commission_entry(self.emp_a, 1200.0).batch_id
        extra = self._batch(self.sup_a, self.emp_a.department_id, component=gm)
        self._entry(extra, self.emp_a, user=self.sup_a, quantity=1.0,
                    amount_override=500.0, reason='بأمر المدير العام')
        run = self._approve(meals)
        self.assertAlmostEqual(
            run.line_ids.filtered(lambda l: l.employee_id == self.emp_a).earnings,
            1700.0)
        # BAS as it was typed: the 500 inside the other line.
        rows = [r for r in run._bas_journal_rows()
                if r['code'] != gm.x_bas_expense_code]
        for r in rows:
            if r['code'] == '3201010006':
                r['debit'] = 1700.0
        self._load(run, self._bas_rows_from(rows))
        voucher = self._export(run)
        out = voucher._bas_rows()
        self.assertEqual(sorted(self._codes(out)), sorted([
            ('3201010006', 0.0, 500.0),
            (gm.x_bas_expense_code, 500.0, 0.0),
        ]))
        self.assertTrue(any('مبلغ إضافي بأمر المدير العام' in r['ref']
                            for r in out))

    def test_04_only_as_the_first_voucher_of_the_month(self):
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        self._export(run)
        wizard = self.env['ksw.pay.bas.baseline.wizard'].create(
            {'run_id': run.id, 'voucher_number': '26008282'})
        with self.assertRaises(UserError):
            wizard.action_preview()


class TestNewVoucherIsKnowing(SettlementCommon):
    """A voucher holds its items, a draft included — so a new one is
    started only through a dialog that shows what came before."""

    def _admin(self):
        return self.env.ref('base.user_admin')

    def _wizard(self):
        return self.env['ksw.pay.settlement.new.wizard'].with_user(
            self._admin()).create({})

    def test_20_a_plain_create_is_refused_once_a_voucher_exists(self):
        self._voucher()
        with self.assertRaises(UserError):
            self.env['ksw.pay.settlement'].with_user(self._admin()).create({})

    def test_21_the_dialog_shows_the_holding_draft_and_the_last_posted(self):
        run = self._approve(self._commission_entry(self.emp_a, 2456.0).batch_id)
        posted = self._export(run)
        self._late_penalty(run, 200.0)
        draft = self._voucher()
        wizard = self._wizard()
        self.assertIn(draft, wizard.draft_ids)
        self.assertEqual(wizard.last_posted_id, posted)
        self.assertEqual(wizard.pending_count, 0, 'the draft holds it')
        with self.assertRaises(UserError):
            wizard.action_create()

    def test_22_confirming_creates_one_carrying_only_what_is_new(self):
        run = self._approve(self._commission_entry(self.emp_a, 2456.0).batch_id)
        self._export(run)
        self._late_penalty(run, 300.0)
        held = self._voucher()
        self._late_penalty(run, 200.0)
        wizard = self._wizard()
        self.assertEqual(wizard.pending_count, 1)
        self.assertAlmostEqual(wizard.pending_deducted, 200.0)
        with self.assertRaises(UserError):
            wizard.action_create()        # not confirmed
        wizard.confirmed = True
        new = self.env['ksw.pay.settlement'].browse(
            wizard.action_create()['res_id'])
        self.assertAlmostEqual(new.total_deducted, 200.0)
        self.assertAlmostEqual(held.total_deducted, 300.0)

    def test_23_discarding_a_draft_gives_its_items_back(self):
        run = self._approve(self._commission_entry(self.emp_a, 2456.0).batch_id)
        self._export(run)
        self._late_penalty(run, 300.0)
        draft = self._voucher()
        self.assertEqual(self._wizard().pending_count, 0)
        draft.with_user(self._admin()).action_discard()
        self.assertFalse(draft.exists())
        self.assertEqual(self._wizard().pending_count, 1)


class TestCancelNotImported(SettlementCommon):

    def test_01_a_file_never_imported_is_taken_back(self):
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        first = self._export(run)
        first.with_user(self.env.ref('base.user_admin')).action_cancel_not_imported()
        self.assertEqual(first.state, 'cancel')
        again = self._export(run, number='26009002')
        self.assertAlmostEqual(again.total_right, 1200.0)

    def test_02_only_the_latest_file_of_the_month(self):
        run = self._approve(self._commission_entry(self.emp_a, 2456.0).batch_id)
        first = self._export(run)
        self._late_penalty(run, 1000.0)
        self._export(run, number='26009002')
        with self.assertRaisesRegex(UserError, 'later files'):
            first.action_cancel_not_imported()

    def test_04_cancelling_the_start_point_allows_loading_again(self):
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        first = self._export(run).with_user(self.env.ref('base.user_admin'))
        run.invalidate_recordset(['x_bas_can_load'])
        self.assertFalse(run.x_bas_can_load)
        first.action_cancel_not_imported()
        run.invalidate_recordset(['x_bas_can_load'])
        self.assertTrue(run.x_bas_can_load)
        wizard = self.env['ksw.pay.bas.baseline.wizard'].create(
            {'run_id': run.id, 'voucher_number': '26008282'})
        wizard._check()      # no longer refused

    def test_05_a_cancelled_bas_load_can_be_loaded_again(self):
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        Wizard = self.env['ksw.pay.bas.baseline.wizard'].with_user(
            self.env.ref('base.user_admin'))
        rows = TestLoadFromBas._bas_rows_from(self, run._bas_journal_rows())
        wizard = Wizard.create({'run_id': run.id, 'voucher_number': '26008282'})
        with patch.object(type(wizard), '_read_bas', lambda w: rows):
            loaded = self.env['ksw.pay.settlement'].browse(
                wizard.action_load()['res_id'])
            loaded.with_user(self.env.ref('base.user_admin')
                             ).action_cancel_not_imported()
            again = Wizard.create({'run_id': run.id, 'voucher_number': '26008282'})
            reloaded = self.env['ksw.pay.settlement'].browse(
                again.action_load()['res_id'])
        self.assertEqual(reloaded.state, 'done')
        self.assertEqual(reloaded.bas_voucher_number, '26008282')

    def test_03_a_cancelled_file_is_sealed(self):
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        first = self._export(run).with_user(self.env.ref('base.user_admin'))
        first.action_cancel_not_imported()
        with self.assertRaises(UserError):
            first.write({'date': first.date + relativedelta(days=1)})
        with self.assertRaises(UserError):
            first.unlink()


class TestCheckAgainstBas(SettlementCommon):
    """A posted file read back from BAS and compared line by line."""

    def _rows_as_bas(self, voucher):
        """What BAS would hold if the file were imported untouched."""
        return [{
            'SERIAL': n, 'FCODE': r['code'] if r['debit'] else None,
            'TCODE': r['code'] if r['credit'] else None,
            'AMOUNT': r['debit'] or r['credit'], 'REF': ' %s ' % r['ref'],
        } for n, r in enumerate(voucher._bas_rows(), 1)]

    def _check(self, voucher, rows):
        Settlement = type(self.env['ksw.pay.settlement'])
        with patch.object(Settlement, '_bas_voucher_rows', lambda s: rows):
            voucher.with_user(self.env.ref('base.user_admin')
                              ).action_check_against_bas()

    def test_01_imported_as_produced_matches(self):
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        voucher = self._export(run)
        self._check(voucher, self._rows_as_bas(voucher))
        self.assertEqual(voucher.bas_check_state, 'match')
        self.assertEqual(voucher.bas_check_diff_count, 0)
        self.assertTrue(voucher.bas_check_date)

    def test_02_the_august_pattern_is_caught(self):
        """A raised line, a block typed by hand, a sales commission in the
        same voucher (KSWCO 26008282)."""
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        voucher = self._export(run)
        rows = self._rows_as_bas(voucher)
        rows[0]['AMOUNT'] = 1700.0          # expense raised by 500
        rows[1]['AMOUNT'] = 1700.0          # and its accrual with it
        rows += [
            {'SERIAL': 90, 'FCODE': '3201010006', 'TCODE': None,
             'AMOUNT': 140.0, 'REF': 'عمولات ردود حسين محمود اغسطس'},
            {'SERIAL': 91, 'FCODE': None, 'TCODE': '2107010001',
             'AMOUNT': 140.0, 'REF': 'عمولات حسين محمود اغسطس'},
            {'SERIAL': 92, 'FCODE': '3203020005', 'TCODE': None,
             'AMOUNT': 742.0, 'REF': 'عمولة تحصيل محمد العزوني اغسطس'},
            {'SERIAL': 93, 'FCODE': None, 'TCODE': '2107010005',
             'AMOUNT': 742.0, 'REF': 'عمولة تحصيل محمد العزوني اغسطس'},
        ]
        self._check(voucher, rows)
        self.assertEqual(voucher.bas_check_state, 'differs')
        # 2 changed + 2 added on its own accounts; the sales lines are shown
        # but not counted against the file.
        self.assertEqual(voucher.bas_check_diff_count, 4)
        report = voucher.bas_check_report
        self.assertIn('1200.00 → 1700.00', report)
        self.assertIn('حسين محمود', report)
        self.assertIn('3203020005', report)

    def test_03_not_in_bas_yet(self):
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        voucher = self._export(run)
        self._check(voucher, [])
        self.assertEqual(voucher.bas_check_state, 'missing')

    def test_04_a_line_left_out_of_bas_is_reported(self):
        run = self._approve(self._commission_entry(self.emp_a, 2456.0).batch_id)
        self._export(run)
        self._late_penalty(run, 1000.0)
        voucher = self._export(run, number='26009002')
        rows = self._rows_as_bas(voucher)[:-1]
        self._check(voucher, rows)
        self.assertEqual(voucher.bas_check_state, 'differs')
        self.assertIn('1205010385', voucher.bas_check_report)

    def test_05_the_daily_job_checks_recent_files_until_they_match(self):
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        voucher = self._export(run)
        Settlement = type(self.env['ksw.pay.settlement'])
        rows = self._rows_as_bas(voucher)
        with patch.object(Settlement, '_bas_voucher_rows',
                          lambda s: rows if s == voucher else []):
            self.env['ksw.pay.settlement']._cron_check_against_bas(commit=False)
        self.assertEqual(voucher.bas_check_state, 'match')


class TestGmAddsToAnApprovedMonth(SettlementCommon):
    """KSWCO August 2026: the GM ordered 500 more for three drivers after
    the month was approved. The lock exempts him; his approval must reach
    the register, or the rows are approved and never paid."""

    def test_01_his_rows_reach_the_register_and_the_journal(self):
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        self.assertEqual(run.state, 'approved')
        self._export(run)
        gm_extra = self.env.ref('KSW_commissions.pay_component_gm_extra')
        # Entered by the administrator (a supervisor is locked out of an
        # approved month); sent and approved by the GM himself.
        batch = self.env['ksw.pay.batch'].sudo().create({
            'component_id': gm_extra.id,
            'department_id': self.emp_a.department_id.id,
            'period': run.period})
        self.env['ksw.pay.entry'].sudo().create({
            'batch_id': batch.id, 'employee_id': self.emp_a.id,
            'quantity': 1.0, 'amount': 500.0, 'reason': 'بأمر المدير العام'})
        batch.with_user(self.gm).action_submit()
        batch.submission_id.with_user(self.gm).action_approve()

        self.assertEqual(run.state, 'approved')
        line = run.line_ids.filtered(lambda l: l.employee_id == self.emp_a)
        self.assertAlmostEqual(line.earnings, 1700.0)
        self.assertIn('approved by the General Manager after',
                      ' '.join(run.message_ids.mapped('body')))
        rows = self._export(run, number='26009002')._bas_rows()
        self.assertEqual(self._codes(rows), [
            (gm_extra.x_bas_expense_code, 500.0, 0.0),
            ('2107010001', 0.0, 500.0),
        ])
        self.assertIn('مبلغ إضافي بأمر المدير العام', rows[0]['ref'])



class TestUnwindKeepsCompletedDeductionsOwed(SettlementCommon):
    """KSWCO PE00208 / PE00209 (Oct 2026): August took the installment on
    approval, payroll completed the deduction, then a later penalty was
    taken from the same unpaid August — the unwind-and-re-apply gave the
    first installment back and never took it again, because the re-apply
    only looks at active deductions."""

    def test_01_a_later_recovery_does_not_drop_a_completed_installment(self):
        ded = self._make_pending_installment(self.emp_a, 2306.0)
        ded.line_ids.sudo().write({'x_awaiting_commission': True})
        run = self._approve(self._commission_entry(self.emp_a, 2456.0).batch_id)
        line = run.line_ids.filtered(lambda l: l.employee_id == self.emp_a)
        self.assertEqual(ded.line_ids.state, 'paid')
        self.assertAlmostEqual(line.loan_offset, 2306.0)
        # Payroll completes it, as the September batch did on KSWCO.
        ded.sudo().write({'state': 'completed'})

        penalty = self._late_penalty(run, 150.0)

        self.assertEqual(ded.line_ids.state, 'paid',
                         'the first installment stays taken')
        self.assertEqual(ded.line_ids.x_paid_via_pay_run_line_id, line)
        self.assertEqual(penalty.line_ids.state, 'paid')
        self.assertAlmostEqual(line.loan_offset, 2456.0)
        self.assertAlmostEqual(line.net_payable, 0.0)

    def test_02_unwinding_reactivates_a_completed_deduction(self):
        ded = self._make_pending_installment(self.emp_a, 500.0)
        ded.line_ids.sudo().write({'x_awaiting_commission': True})
        run = self._approve(self._commission_entry(self.emp_a, 1200.0).batch_id)
        ded.sudo().write({'state': 'completed'})
        run.sudo().line_ids.filtered(lambda l: l.employee_id == self.emp_a
                                     )._unwind_loan_offset()
        self.assertEqual(ded.line_ids.state, 'pending')
        self.assertEqual(ded.state, 'active', 'owed again, so active again')
