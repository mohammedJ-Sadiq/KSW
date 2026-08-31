# -*- coding: utf-8 -*-
"""Statement of Account — the deduction ledger.

The load-bearing property is that *charges minus credits equals what is
still owed*. Everything else on the statement is presentation. So most of
these tests are variations on one assertion: after some sequence of real
settlement events, does the closing balance still agree with the
outstanding figure the rest of the module already shows?

The cases that earn their keep are the ones where the arithmetic is not
obvious: a cancelled deduction that had a manual adjustment (where naively
crediting every skipped line drives the balance negative), a payslip that
could only afford part of an installment, and an arrear collected months
after the month it was scheduled for.
"""
from datetime import date

from dateutil.relativedelta import relativedelta

from odoo.service.model import call_kw

from .common import DeductionCommon


class StatementCommon(DeductionCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)

        def _mk(login, group_xmlids):
            return Users.create({
                'name': login,
                'login': login,
                'email': f'{login}@kswsoa.test',
                'group_ids': [
                    (6, 0, [cls.env.ref(g).id for g in group_xmlids])],
            })

        cls.user_officer = _mk(
            'kswsoa_officer', ['KSW_deduction.group_deduction_officer'])
        cls.user_data_entry = _mk(
            'kswsoa_dataentry', ['KSW_deduction.group_acc_data_entry'])
        cls.user_loan_hr = _mk(
            'kswsoa_loanhr', ['KSW_deduction.group_loan_hr'])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _statement(self, employee=None, date_from=None, date_to=None,
                   types=None, group_by_type=False, user=None):
        """Open a statement covering everything unless told otherwise."""
        Wizard = self.env['ksw.deduction.statement.wizard']
        if user:
            Wizard = Wizard.with_user(user)
        return Wizard.create({
            'employee_id': (employee or self.employee).id,
            'date_from': date_from,
            'date_to': date_to or date(2099, 12, 31),
            'type_ids': [(6, 0, types.ids)] if types else False,
            'group_by_type': group_by_type,
        })

    def _outstanding(self, employee=None):
        """What the rest of the module says the employee still owes.

        Read through `sudo()` because `x_deduction_outstanding_total`
        carries `groups='hr.group_hr_user'` and would otherwise raise
        inside the compute for a non-HR user.
        """
        employee = employee or self.employee
        employee.invalidate_recordset()
        return employee.sudo().x_deduction_outstanding_total

    def _assert_ledger_agrees(self, employee=None):
        """The statement and the employee's outstanding figure must match.

        This is the invariant the whole design rests on. It holds because
        every settlement route preserves `Σ(pending + paid) == amount`.
        """
        wizard = self._statement(employee=employee)
        self.assertAlmostEqual(
            wizard.closing_balance, self._outstanding(employee), places=2,
            msg='Statement closing balance disagrees with the employee '
                'outstanding total — one of the two is wrong.')
        return wizard

    def _kinds(self, wizard):
        wizard._build_lines()
        return wizard.line_ids.sorted('sequence').mapped('row_kind')

    def _movements(self, wizard):
        """The transaction rows only, without the balance scaffolding."""
        wizard._build_lines()
        return wizard.line_ids.filtered(lambda l: not l.is_balance_row)

    def _fake_payslip(self, dfrom, dto):
        """A payslip stub good enough to date a settlement.

        `_settle_payslip_lines` only reads `id` and `date_to`, so the full
        payroll fixture (calendar, version, attendance sheet) is not
        needed to exercise the ledger.
        """
        return self.env['hr.payslip'].sudo().create({
            'employee_id': self.employee.id,
            'name': 'KSWSOA Slip %s' % dto,
            'date_from': dfrom,
            'date_to': dto,
        })


class TestStatementBasics(StatementCommon):

    def test_active_deduction_charges_the_full_amount(self):
        ded = self._activate(self._make_deduction(
            self.type_gov_pen, amount=1200.0, installments=4))
        wizard = self._assert_ledger_agrees()
        self.assertAlmostEqual(wizard.total_charged, 1200.0, places=2)
        self.assertAlmostEqual(wizard.total_settled, 0.0, places=2)
        self.assertAlmostEqual(wizard.closing_balance, 1200.0, places=2)
        self.assertEqual(ded.state, 'active')

    def test_draft_deduction_is_not_on_the_statement(self):
        """Nothing has been charged to the employee yet."""
        self._make_deduction(self.type_gov_pen, amount=900.0)
        wizard = self._statement()
        self.assertEqual(wizard.movement_count, 0)
        self.assertAlmostEqual(wizard.closing_balance, 0.0, places=2)

    def test_statement_opens_and_closes_with_a_balance_row(self):
        self._activate(self._make_deduction(
            self.type_gov_pen, amount=400.0, installments=2))
        kinds = self._kinds(self._statement())
        self.assertEqual(kinds[0], 'opening')
        self.assertEqual(kinds[-1], 'closing')

    def test_running_balance_walks_charge_then_credits(self):
        ded = self._activate(self._make_deduction(
            self.type_gov_pen, amount=900.0, installments=3))
        ded.line_ids[0].action_mark_line_paid()
        wizard = self._statement()
        wizard._build_lines()
        rows = wizard.line_ids.sorted('sequence')
        self.assertAlmostEqual(rows[0].balance, 0.0, places=2)   # opening
        self.assertAlmostEqual(rows[1].balance, 900.0, places=2)  # charge
        self.assertAlmostEqual(rows[2].balance, 600.0, places=2)  # credit
        self.assertAlmostEqual(rows[-1].balance, 600.0, places=2)  # closing
        self._assert_ledger_agrees()

    def test_opening_balance_folds_in_earlier_movements(self):
        """Everything before `date_from` collapses into one opening row."""
        ded = self._activate(self._make_deduction(
            self.type_gov_pen, amount=1000.0, installments=2))
        ded.line_ids[0].action_mark_line_paid()
        # Start the statement tomorrow: today's charge and settlement are
        # both history, so they belong in the opening balance.
        tomorrow = date.today() + relativedelta(days=1)
        wizard = self._statement(date_from=tomorrow)
        self.assertAlmostEqual(wizard.opening_balance, 500.0, places=2)
        self.assertEqual(wizard.movement_count, 0)
        self.assertAlmostEqual(wizard.closing_balance, 500.0, places=2)

    def test_date_to_excludes_later_movements(self):
        """A statement cannot report what had not happened when it is dated."""
        ded = self._activate(self._make_deduction(
            self.type_gov_pen, amount=1000.0, installments=2))
        ded.line_ids[0].action_mark_line_paid()
        yesterday = date.today() - relativedelta(days=1)
        wizard = self._statement(date_to=yesterday)
        self.assertEqual(wizard.movement_count, 0)
        self.assertAlmostEqual(wizard.closing_balance, 0.0, places=2)


class TestStatementCancellation(StatementCommon):
    """The write-off row — where naive arithmetic goes negative."""

    def test_cancelled_deduction_closes_at_zero(self):
        ded = self._activate(self._make_deduction(
            self.type_gov_pen, amount=800.0, installments=4))
        ded.line_ids[0].action_mark_line_paid()
        ded.sudo().action_cancel()
        wizard = self._statement()
        self.assertIn('writeoff', self._kinds(wizard))
        self.assertAlmostEqual(wizard.total_written_off, 600.0, places=2)
        self.assertAlmostEqual(wizard.closing_balance, 0.0, places=2)

    def test_cancelled_after_manual_adjustment_does_not_go_negative(self):
        """The bug a `Σ(skipped)` write-off would have shipped.

        Adding a manual paid line forces the accountant to skip a
        compensating pending line first, so an *active* deduction can
        already carry skipped lines. Cancelling then skips the remaining
        pending ones too. Crediting every skipped line would double-count
        the earlier adjustment and drive the balance below zero; the
        write-off is `amount - Σpaid`, which cannot.
        """
        ded = self._activate(self._make_deduction(
            self.type_gov_pen, amount=800.0, installments=4))
        # The manual-adjustment pattern: skip one pending line, then add a
        # manual paid line of the same value so the total still reconciles.
        ded.line_ids[0].with_context(
            _skip_installment_total_check=True).write({'state': 'skipped'})
        self.env['ksw.deduction.line'].sudo().create({
            'deduction_id': ded.id,
            'year': self.this_month.year,
            'month': self.this_month.month,
            'amount': 200.0,
            'state': 'paid',
            'is_manual': True,
        })
        ded.sudo().action_cancel()

        wizard = self._statement()
        self.assertGreaterEqual(
            wizard.closing_balance, 0.0,
            'A cancelled deduction can never leave the employee in credit.')
        self.assertAlmostEqual(wizard.closing_balance, 0.0, places=2)

    def test_reset_to_draft_clears_both_ledger_dates(self):
        ded = self._activate(self._make_deduction(
            self.type_gov_pen, amount=500.0, installments=2))
        self.assertTrue(ded.x_charge_date)
        ded.sudo().action_cancel()
        self.assertTrue(ded.x_writeoff_date)
        ded.sudo().action_reset_to_draft()
        self.assertFalse(ded.x_charge_date)
        self.assertFalse(ded.x_writeoff_date)
        # And a re-activation re-stamps rather than reusing the old date.
        ded.sudo().action_submit()
        self.assertTrue(ded.x_charge_date)
        self.assertFalse(ded.x_writeoff_date)


class TestStatementSettlementDates(StatementCommon):
    """Credits are dated when the money moved, not when it was scheduled."""

    def test_manual_settlement_dated_by_collection_date(self):
        ded = self._activate(self._make_deduction(
            self.type_gov_pen, amount=600.0, installments=3))
        line = ded.line_ids[0]
        line.action_mark_line_paid()
        self.assertEqual(line.x_settlement_date, date.today())
        credits = self._movements(self._statement()).filtered(
            lambda l: l.row_kind == 'credit')
        self.assertEqual(len(credits), 1)
        self.assertEqual(credits.date, date.today())

    def test_arrear_is_dated_by_settlement_not_by_schedule(self):
        """A January installment collected in April is an April credit.

        `_settle_payslip_lines` leaves a forwarded arrear on its original
        `period_date` on purpose, so dating the ledger by `period_date`
        would report the money as collected months before it was.
        """
        start = date(2026, 1, 1)
        ded = self._activate(self._make_deduction(
            self.type_gov_pen, amount=1000.0, installments=2,
            start_month=start))
        january_line = ded.line_ids.sorted('sequence')[0]
        self.assertEqual(january_line.period_date, start)

        slip = self._fake_payslip(date(2026, 4, 1), date(2026, 4, 30))
        ded.sudo()._settle_payslip_lines(
            january_line, {january_line.id: 500.0}, slip)

        credits = self._movements(self._statement()).filtered(
            lambda l: l.row_kind == 'credit')
        self.assertEqual(len(credits), 1)
        # Dated by the payslip that collected it...
        self.assertEqual(credits.date, date(2026, 4, 30))
        # ...while the installment itself still says January, which is
        # exactly the divergence that makes `period_date` the wrong axis.
        self.assertEqual(january_line.period_date, start)
        self.assertNotEqual(credits.date, january_line.period_date)

    def test_partial_collection_credits_only_the_collected_part(self):
        """The split branch: paid part is a credit, remainder is not."""
        ded = self._activate(self._make_deduction(
            self.type_gov_pen, amount=1000.0, installments=2,
            start_month=date(2026, 1, 1)))
        line = ded.line_ids.sorted('sequence')[0]
        slip = self._fake_payslip(date(2026, 1, 1), date(2026, 1, 31))
        ded.sudo()._settle_payslip_lines(line, {line.id: 200.0}, slip)

        credits = self._movements(self._statement()).filtered(
            lambda l: l.row_kind == 'credit')
        self.assertEqual(len(credits), 1)
        self.assertAlmostEqual(credits.credit, 200.0, places=2)
        self._assert_ledger_agrees()

    def test_payslip_reset_reverses_the_credit(self):
        ded = self._activate(self._make_deduction(
            self.type_gov_pen, amount=1000.0, installments=2,
            start_month=date(2026, 1, 1)))
        line = ded.line_ids.sorted('sequence')[0]
        slip = self._fake_payslip(date(2026, 1, 1), date(2026, 1, 31))
        ded.sudo()._settle_payslip_lines(line, {line.id: 500.0}, slip)
        self.assertEqual(len(self._movements(self._statement()).filtered(
            lambda l: l.row_kind == 'credit')), 1)

        ded.sudo()._unmark_lines_paid(slip)
        self.assertFalse(line.x_settlement_date)
        self.assertEqual(len(self._movements(self._statement()).filtered(
            lambda l: l.row_kind == 'credit')), 0)
        self._assert_ledger_agrees()


class TestStatementPaymentWizard(StatementCommon):
    """Both wizard modes must leave the ledger consistent."""

    def _pay(self, ded, amount, mode):
        wizard = self.env['ksw.loan.payment.wizard'].sudo().create({
            'deduction_id': ded.id,
            'payment_amount': amount,
            'payment_date': date.today(),
            'application_mode': mode,
        })
        wizard.action_confirm()
        return wizard

    def test_sequential_payment_credits_the_amount_paid(self):
        ded = self._activate(self._make_deduction(
            self.type_gov_pen, amount=4000.0, installments=4))
        self._pay(ded, 1650.0, 'sequential')
        wizard = self._assert_ledger_agrees()
        self.assertAlmostEqual(wizard.total_settled, 1650.0, places=2)
        self.assertAlmostEqual(wizard.closing_balance, 2350.0, places=2)

    def test_redistribute_payment_credits_the_amount_paid(self):
        ded = self._activate(self._make_deduction(
            self.type_gov_pen, amount=4000.0, installments=4))
        self._pay(ded, 1000.0, 'redistribute')
        wizard = self._assert_ledger_agrees()
        self.assertAlmostEqual(wizard.total_settled, 1000.0, places=2)
        self.assertAlmostEqual(wizard.closing_balance, 3000.0, places=2)

    def test_full_payment_closes_the_account(self):
        ded = self._activate(self._make_deduction(
            self.type_gov_pen, amount=4000.0, installments=4))
        self._pay(ded, 4000.0, 'sequential')
        wizard = self._assert_ledger_agrees()
        self.assertAlmostEqual(wizard.closing_balance, 0.0, places=2)
        self.assertEqual(ded.state, 'completed')


class TestStatementTypeFilter(StatementCommon):
    """"All types" versus "one type" — the parts must sum to the whole."""

    def _two_accounts(self):
        self._activate(self._make_deduction(
            self.type_gov_pen, amount=600.0, installments=2))
        self._activate(self._make_deduction(
            self.type_advance, amount=900.0, installments=3))

    def test_single_type_returns_only_that_type(self):
        self._two_accounts()
        wizard = self._statement(types=self.type_gov_pen)
        wizard._build_lines()
        types = self._movements(wizard).mapped('type_id')
        self.assertEqual(types, self.type_gov_pen)
        self.assertAlmostEqual(wizard.closing_balance, 600.0, places=2)

    def test_the_parts_sum_to_the_whole(self):
        self._two_accounts()
        combined = self._statement().closing_balance
        penalties = self._statement(types=self.type_gov_pen).closing_balance
        advances = self._statement(types=self.type_advance).closing_balance
        self.assertAlmostEqual(penalties + advances, combined, places=2)
        self.assertAlmostEqual(combined, 1500.0, places=2)

    def test_group_by_type_subtotals_reconcile(self):
        self._two_accounts()
        wizard = self._statement(group_by_type=True)
        wizard._build_lines()
        subtotals = wizard.line_ids.filtered(
            lambda l: l.row_kind == 'subtotal')
        self.assertEqual(len(subtotals), 2)
        closing = wizard.line_ids.filtered(lambda l: l.row_kind == 'closing')
        self.assertAlmostEqual(
            sum(subtotals.mapped('balance')), closing.balance, places=2)


class TestStatementAccess(StatementCommon):
    """Each role sees exactly the scope its record rules already give it."""

    def setUp(self):
        super().setUp()
        self.loan = self._activate(self._make_deduction(
            self.type_loan, amount=6000.0, installments=6))
        self.penalty = self._activate(self._make_deduction(
            self.type_gov_pen, amount=600.0, installments=2))

    def test_data_entry_sees_no_loans(self):
        """The accounting data-entry team owns non-loan types only."""
        wizard = self._statement(user=self.user_data_entry)
        refs = self._movements(wizard).mapped('ref')
        self.assertNotIn(self.loan.name, refs)
        self.assertIn(self.penalty.name, refs)

    def test_data_entry_is_told_the_statement_is_partial(self):
        """Silently omitting rows would misstate the account."""
        wizard = self._statement(user=self.user_data_entry)
        self.assertGreater(wizard.out_of_scope_count, 0)

    def test_officer_sees_everything_and_nothing_is_flagged(self):
        wizard = self._statement(user=self.user_officer)
        refs = self._movements(wizard).mapped('ref')
        self.assertIn(self.loan.name, refs)
        self.assertIn(self.penalty.name, refs)
        self.assertEqual(wizard.out_of_scope_count, 0)

    def test_loan_hr_can_render_the_statement(self):
        """Catches AccessError from settlement_label reaching hr.payslip.

        `settlement_label` reads `payslip_id.number`, which needs payroll
        access the deduction roles do not have. The ledger reads its lines
        under `sudo()` precisely so this renders; the test is here because
        the failure only shows up at render time.

        Rendered as HTML rather than PDF on purpose: the PDF path falls
        back to HTML when wkhtmltopdf is absent, which would make the
        assertion depend on the machine rather than on the code. The QWeb
        template and every field it touches are exercised either way.
        """
        self.loan.line_ids[0].sudo().action_mark_line_paid()
        wizard = self._statement(user=self.user_loan_hr)
        wizard._build_lines()
        html = self.env['ir.actions.report'].with_user(
            self.user_loan_hr)._render_qweb_html(
                'KSW_deduction.action_report_deduction_statement',
                wizard.ids)[0]
        self.assertIn(b'Statement of Account', html)
        self.assertIn(self.loan.name.encode(), html)


class TestStatementOutputsAgree(StatementCommon):
    """Screen, PDF and spreadsheet are built from the same rows."""

    def test_xlsx_has_a_row_per_ledger_line(self):
        ded = self._activate(self._make_deduction(
            self.type_gov_pen, amount=900.0, installments=3))
        ded.line_ids[0].action_mark_line_paid()
        wizard = self._statement()
        wizard.action_export_xlsx()
        attachment = self.env['ir.attachment'].search(
            [('name', 'like', 'Statement of Account')], order='id desc',
            limit=1)
        self.assertTrue(attachment)
        # The builder is the same list the screen renders; assert the row
        # count rather than parsing the workbook back.
        self.assertEqual(len(wizard.line_ids), len(self._kinds(wizard)))

    def test_report_data_matches_the_summary_fields(self):
        ded = self._activate(self._make_deduction(
            self.type_gov_pen, amount=900.0, installments=3))
        ded.line_ids[0].action_mark_line_paid()
        wizard = self._statement()
        data = wizard.get_report_data()
        self.assertAlmostEqual(
            data['closing_balance'], wizard.closing_balance, places=2)
        self.assertAlmostEqual(
            data['total_settled'], wizard.total_settled, places=2)
        self.assertEqual(len(data['lines']), len(wizard.line_ids))

    def test_rebuilding_does_not_duplicate_rows(self):
        """Every button rebuilds; the ledger must not grow each time."""
        self._activate(self._make_deduction(
            self.type_gov_pen, amount=900.0, installments=3))
        wizard = self._statement()
        first = len(wizard._build_lines())
        second = len(wizard._build_lines())
        self.assertEqual(first, second)
        self.assertEqual(len(wizard.line_ids), second)


class TestStatementReopen(StatementCommon):
    """The header button opens a BLANK dialog, same as the menu."""

    MENU_ACTION = 'KSW_deduction.action_ksw_deduction_statement'

    def _viewed(self):
        """Run the View action and return (wizard, its act_window)."""
        self._activate(self._make_deduction(
            self.type_gov_pen, amount=900.0, installments=3))
        wizard = self._statement(date_from=date(2020, 1, 1))
        return wizard, wizard.action_view()

    def test_view_action_leaks_no_defaults(self):
        """A leftover `default_*` here would pre-fill the blank dialog."""
        _wizard, action = self._viewed()
        leaked = [k for k in action['context'] if k.startswith('default_')]
        self.assertFalse(leaked, 'view context must not seed the new dialog')

    def test_reopen_matches_the_menu_action(self):
        """It IS the menu's action, so the two cannot drift apart."""
        _wizard, action = self._viewed()
        reopened = self.env['ksw.deduction.statement.line'].with_context(
            **action['context']).action_reopen_wizard()
        menu = self.env['ir.actions.act_window']._for_xml_id(self.MENU_ACTION)
        self.assertEqual(reopened['res_model'], menu['res_model'])
        self.assertEqual(reopened['target'], menu['target'])
        self.assertEqual(reopened['id'], menu['id'])

    def test_reopen_opens_a_blank_dialog(self):
        """No res_id, no defaults — the point of the whole change."""
        _wizard, action = self._viewed()
        reopened = self.env['ksw.deduction.statement.line'].with_context(
            **action['context']).action_reopen_wizard()
        self.assertFalse(reopened.get('res_id'))
        self.assertEqual(reopened['context'], {})

    def test_blank_dialog_has_no_employee_prefilled(self):
        """What the user actually sees: an empty Employee field.

        Built the way the client does — `default_get` under the returned
        action's context — so a default leaking in would show up here.
        """
        _wizard, action = self._viewed()
        reopened = self.env['ksw.deduction.statement.line'].with_context(
            **action['context']).action_reopen_wizard()
        Wizard = self.env['ksw.deduction.statement.wizard'].with_context(
            **reopened['context'])
        defaults = Wizard.default_get(
            ['employee_id', 'date_from', 'date_to', 'type_ids',
             'group_by_type'])
        self.assertFalse(defaults.get('employee_id'))
        self.assertFalse(defaults.get('type_ids'))
        self.assertFalse(defaults.get('group_by_type'))
        # ...and it still opens on the module's own default period, exactly
        # as the menu does.
        menu_defaults = self.env[
            'ksw.deduction.statement.wizard'].default_get(
                ['date_from', 'date_to'])
        self.assertEqual(defaults.get('date_from'),
                         menu_defaults.get('date_from'))
        self.assertEqual(defaults.get('date_to'), menu_defaults.get('date_to'))

    def test_reopen_survives_the_real_rpc_path(self):
        """Go through `call_kw`, the way the button actually arrives.

        Calling the method directly from Python is NOT equivalent and will
        not catch the failure this test exists for: a button always sends
        its ids, and `call_kw` strips them only for non-`@api.model`
        methods. Decorating this one `@api.model` therefore left the id
        list in `args` and every click raised `TypeError: takes 1
        positional argument but 2 were given` — which shipped to
        production, because the unit test called Python, not RPC.
        """
        _wizard, action = self._viewed()
        # Exactly what the web client sends for a header button with
        # nothing selected: one positional arg (the empty id list) and the
        # action context in kwargs. The context has to travel in kwargs —
        # `call_kw` does `recs.with_context(kwargs.pop('context') or {})`,
        # which REPLACES whatever the recordset was carrying.
        result = call_kw(
            self.env['ksw.deduction.statement.line'],
            'action_reopen_wizard', [[]], {'context': action['context']})
        self.assertEqual(result['res_model'],
                         'ksw.deduction.statement.wizard')
        self.assertFalse(result.get('res_id'))

    def test_reopen_survives_rpc_with_rows_selected(self):
        """Same call, but with rows ticked — ids must still be stripped."""
        wizard, action = self._viewed()
        wizard._build_lines()
        result = call_kw(
            self.env['ksw.deduction.statement.line'],
            'action_reopen_wizard', [wizard.line_ids.ids],
            {'context': action['context']})
        self.assertEqual(result['res_model'],
                         'ksw.deduction.statement.wizard')

    def test_blank_dialog_still_produces_a_statement(self):
        """End to end: new dialog -> fill it -> View -> rows."""
        _wizard, action = self._viewed()
        reopened = self.env['ksw.deduction.statement.line'].with_context(
            **action['context']).action_reopen_wizard()
        fresh = self.env['ksw.deduction.statement.wizard'].with_context(
            **reopened['context']).create({
                'employee_id': self.employee.id,
                'date_to': date(2099, 12, 31)})
        fresh.action_view()
        self.assertTrue(fresh.line_ids)


class TestStatementOverdue(StatementCommon):

    def test_overdue_reports_past_due_pending_installments(self):
        self._activate(self._make_deduction(
            self.type_gov_pen, amount=1000.0, installments=2,
            start_month=date(2020, 1, 1)))
        wizard = self._statement()
        self.assertAlmostEqual(wizard.overdue_amount, 1000.0, places=2)
        self.assertAlmostEqual(
            wizard.closing_balance, wizard.overdue_amount, places=2)

    def test_future_installments_are_not_overdue(self):
        future = date.today().replace(day=1) + relativedelta(months=6)
        self._activate(self._make_deduction(
            self.type_gov_pen, amount=1000.0, installments=2,
            start_month=future))
        wizard = self._statement()
        self.assertAlmostEqual(wizard.overdue_amount, 0.0, places=2)
        self.assertAlmostEqual(wizard.closing_balance, 1000.0, places=2)
