"""Commission entries are paid on the vacation payslip, not re-typed.

The deductions twin: a vacation settles the employee's whole position at the
request, and what he is still owed in commissions is already recorded as
``ksw.pay.entry`` rows. The settlement reads them:

  * every approved entry up to and including the departure month, unless
    that month's own run was marked Paid with a line for him; an approved
    but unpaid month is paid here and taken out of its register;
  * one ``KSW_COM_<entry_id>`` input each, summed by ``KSW_COMMISSIONS``;
  * confirming the payslip stamps them paid, and the monthly run leaves a
    stamped entry out of its register; cancelling it releases them.

Worked example: an unpaid leave from 17 Aug. July and August are settled on
it; September is not the settlement's business. (2031, so no real pay
run on the dev database locks the months.)
"""
from datetime import date

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

JUL = date(2031, 7, 1)
AUG = date(2031, 8, 1)
SEP = date(2031, 9, 1)


class _SettlementCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.calendar = env['resource.calendar'].create({
            'name': 'Commission Settlement Calendar', 'tz': 'Asia/Riyadh'})
        cls.dept = env['hr.department'].create({'name': 'Settlement Dept'})
        cls.employee = env['hr.employee'].create({
            'name': 'Settlement Driver',
            'department_id': cls.dept.id,
            'resource_calendar_id': cls.calendar.id,
            'tz': 'Asia/Riyadh',
            'country_id': env.ref('base.sa').id,
        })
        cls.version = cls.employee.current_version_id
        cls.version.write({
            'date_version': date(2025, 1, 1),
            'contract_date_start': date(2025, 1, 1),
            'resource_calendar_id': cls.calendar.id,
            'wage': 6000.0,
            'struct_id': env.ref('om_hr_payroll.structure_base').id,
        })
        cls.employee._compute_current_version_id()
        cls.colleague = env['hr.employee'].create({
            'name': 'Settlement Colleague', 'department_id': cls.dept.id})

        cls.unpaid_type = env['hr.leave.type'].create({
            'name': 'Settlement Unpaid Leave',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'request_unit': 'day',
            'is_unpaid_leave': True,
        })
        cls.plain_type = env['hr.leave.type'].create({
            'name': 'Settlement Sick Leave',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'request_unit': 'day',
        })
        cls.component = env.ref('KSW_commissions.pay_component_overtime')

        cls.jul = cls._entry(JUL, 700.0)
        cls.aug = cls._entry(AUG, 300.0)
        cls.sep = cls._entry(SEP, 999.0)
        # Signed off by the GM — the only figures a settlement may pay.
        (cls.jul | cls.aug | cls.sep).batch_id.write({'state': 'approved'})

    # ------------------------------------------------------------------
    @classmethod
    def _batch(cls, period):
        batch = cls.env['ksw.pay.batch'].sudo().search([
            ('period', '=', period), ('department_id', '=', cls.dept.id)],
            limit=1)
        return batch or cls.env['ksw.pay.batch'].sudo().create({
            'component_id': cls.component.id,
            'department_id': cls.dept.id,
            'period': period,
        })

    @classmethod
    def _entry(cls, period, amount, employee=None):
        batch = cls._batch(period)
        vals = {
            'batch_id': batch.id,
            'employee_id': (employee or cls.employee).id,
            'quantity': 2.0,
            'reason': 'settlement probe',
            'amount_override': amount,
        }
        if batch.component_id.needs_date:
            vals['date'] = period.replace(day=5)
        return cls.env['ksw.pay.entry'].sudo().create(vals)

    def _leave(self, leave_type=None, date_from=date(2031, 8, 17),
               date_to=date(2031, 8, 31)):
        return self.env['hr.leave'].with_context(
            tracking_disable=True, mail_create_nosubscribe=True,
        ).create({
            'employee_id': self.employee.id,
            'holiday_status_id': (leave_type or self.unpaid_type).id,
            'request_date_from': date_from,
            'request_date_to': date_to,
        })

    def _run(self, period):
        # Creating a batch already opened the month's run (its submission
        # does), and a period has exactly one.
        return self.env['ksw.pay.run'].sudo().search(
            [('period', '=', period)], limit=1) or \
            self.env['ksw.pay.run'].sudo().create({'period': period})

    def _run_line(self, period, employee, earnings, state):
        run = self._run(period)
        self.env['ksw.pay.run.line'].sudo().create({
            'run_id': run.id, 'employee_id': employee.id,
            'earnings': earnings})
        run.write({'state': state})
        return run

    def _payslip(self, leave, **extra):
        payslip = self.env['hr.payslip'].create(dict({
            'employee_id': self.employee.id,
            'date_from': AUG,
            'date_to': date(2031, 8, 31),
            'version_id': self.version.id,
            'struct_id': self.version.struct_id.id,
            'x_leave_id': leave.id,
        }, **extra))
        vals = self.env['hr.leave']._build_vacation_input_lines(
            leave, self.employee, payslip)
        self.env['hr.payslip.input'].create(vals)
        payslip.compute_sheet()
        return payslip

    @staticmethod
    def _com_inputs(payslip):
        return {
            i.code: i.amount for i in payslip.input_line_ids
            if i.code and i.code.startswith('KSW_COM_')}


class TestVacationCommissionSettlement(_SettlementCommon):

    # ------------------------------------------------------------------
    # Which entries
    # ------------------------------------------------------------------
    def test_months_up_to_departure_are_settled(self):
        entries = self._leave()._commission_entries_to_settle()
        self.assertEqual(entries, self.jul | self.aug,
                         'September starts after he left: not the '
                         'settlement\'s business.')

    def test_unapproved_entries_are_listed_but_not_paid(self):
        """KSWCO dev leave 47147: a submitted, never-approved batch was
        paid on the vacation payslip."""
        self.aug.batch_id.write({'state': 'submitted'})
        leave = self._leave()
        self.assertEqual(leave._commission_entries_to_settle(), self.jul)
        self.assertEqual(leave.x_commission_entries_total, 700.0)
        self.assertEqual(leave.x_commission_pending_count, 1)
        self.assertEqual(leave.x_commission_pending_total, 300.0)
        payslip = self._payslip(leave)
        self.assertEqual(self._com_inputs(payslip),
                         {'KSW_COM_%d' % self.jul.id: 700.0})

    def test_confirming_refuses_an_entry_approved_nowhere(self):
        """A payslip built earlier and confirmed by hand after the batch was
        taken back is refused, not paid."""
        payslip = self._payslip(self._leave())
        self.aug.batch_id.write({'state': 'draft'})
        with self.assertRaises(UserError):
            payslip.write({'state': 'done'})

    def test_month_already_paid_by_its_run_is_left_alone(self):
        self._run_line(JUL, self.employee, 700.0, 'paid')
        entries = self._leave()._commission_entries_to_settle()
        self.assertEqual(entries, self.aug)

    def test_approved_but_unpaid_month_is_settled(self):
        """KSWCO leave 5185: August was approved (register built, 800 on
        his line) but the bank transfer goes out about a month later. The
        vacation settles his account on the day he leaves, so it pays
        August now."""
        self._run_line(JUL, self.employee, 700.0, 'approved')
        entries = self._leave()._commission_entries_to_settle()
        self.assertEqual(entries, self.jul | self.aug)

    def test_paid_run_that_did_not_pay_him_is_still_settled(self):
        """A run marked Paid with no line for him paid him nothing."""
        self._run_line(JUL, self.colleague, 50.0, 'paid')
        entries = self._leave()._commission_entries_to_settle()
        self.assertEqual(entries, self.jul | self.aug)

    def test_confirming_takes_him_out_of_the_approved_register(self):
        """Paid on the vacation payslip: the approved month's bank file
        must not pay him again. Cancelling gives the line back."""
        self.jul.batch_id.submission_id.sudo().write({'state': 'approved'})
        run = self._run_line(JUL, self.employee, 700.0, 'approved')
        self.env['ksw.pay.run.line'].sudo().create({
            'run_id': run.id, 'employee_id': self.colleague.id,
            'earnings': 50.0})
        payslip = self._payslip(self._leave())
        payslip.write({'state': 'done'})
        self.assertEqual(self.jul.x_vacation_payslip_id, payslip)
        paid = {l.employee_id: l.earnings for l in run.line_ids}
        self.assertNotIn(self.employee, paid)
        self.assertEqual(paid.get(self.colleague), 50.0,
                         'Nobody else on the register is touched.')

        payslip.write({'state': 'cancel'})
        paid = {l.employee_id: l.earnings for l in run.line_ids}
        self.assertEqual(paid.get(self.employee), 700.0)

    def test_eos_request_settles_and_stamps(self):
        # KSWCO leave 5218: the EOS request listed no commissions at all.
        if 'is_eos_leave' not in self.env['hr.leave.type']._fields:
            self.skipTest('KSW_eos_leave not installed')
        eos_type = self.env['hr.leave.type'].create({
            'name': 'Settlement EOS',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'request_unit': 'day',
            'is_eos_leave': True,
        })
        leave = self._leave(eos_type, date_to=date(2031, 8, 17))
        self.assertTrue(leave.x_commission_entries_applies)
        self.assertEqual(leave._commission_entries_to_settle(),
                         self.jul | self.aug)
        payslip = self._payslip(leave)
        payslip.write({'state': 'done'})
        self.assertEqual((self.jul | self.aug).x_vacation_payslip_id, payslip,
                         'Stamped, so the monthly run leaves them out.')

    def test_other_leave_types_pull_nothing(self):
        leave = self._leave(self.plain_type)
        self.assertFalse(leave._commission_entries_to_settle())
        self.assertFalse(leave.x_commission_entries_applies)

    # ------------------------------------------------------------------
    # The payslip
    # ------------------------------------------------------------------
    def test_payslip_carries_one_input_per_entry(self):
        payslip = self._payslip(self._leave())
        self.assertEqual(self._com_inputs(payslip), {
            'KSW_COM_%d' % self.jul.id: 700.0,
            'KSW_COM_%d' % self.aug.id: 300.0,
        })
        line = payslip.line_ids.filtered(lambda l: l.code == 'KSW_COMMISSIONS')
        self.assertEqual(sum(line.mapped('total')), 1000.0)

    def test_confirming_stamps_and_cancelling_releases(self):
        payslip = self._payslip(self._leave())
        payslip.write({'state': 'done'})
        self.assertEqual(self.jul.x_vacation_payslip_id, payslip)
        self.assertEqual(self.aug.x_vacation_payslip_id, payslip)
        self.assertFalse(self.sep.x_vacation_payslip_id)
        self.assertTrue(self.aug.x_vacation_settlement)

        payslip.write({'state': 'cancel'})
        self.assertFalse((self.jul | self.aug).x_vacation_payslip_id,
                         'A cancelled settlement paid nothing: the run '
                         'may pay them again.')

    def test_preview_settles_nothing(self):
        payslip = self._payslip(self._leave(), x_is_vacation_preview=True)
        self.assertTrue(self._com_inputs(payslip))
        payslip.write({'state': 'done'})
        self.assertFalse((self.jul | self.aug).x_vacation_payslip_id)

    def test_a_settled_entry_is_not_pulled_twice(self):
        first = self._leave()
        self._payslip(first).write({'state': 'done'})
        # Back on 1 Sep — without that the first request is still open and
        # holds September as a month he was away, which is right.
        first.sudo().write({'x_return_date': date(2031, 9, 1),
                            'x_return_state': 'hr_confirmed'})
        # A second vacation later on: July and August are already paid.
        later = self._leave(date_from=date(2031, 9, 25),
                            date_to=date(2031, 9, 30))
        self.assertEqual(later._commission_entries_to_settle(), self.sep)

    # ------------------------------------------------------------------
    # After it is paid
    # ------------------------------------------------------------------
    def test_a_paid_entry_is_locked_even_under_sudo(self):
        self._payslip(self._leave()).write({'state': 'done'})
        with self.assertRaises(UserError):
            self.aug.sudo().write({'amount_override': 1.0})
        with self.assertRaises(UserError):
            self.aug.sudo().unlink()
        with self.assertRaises(UserError):
            self.aug.batch_id.sudo().unlink()

    def test_the_register_does_not_pay_it_again(self):
        other = self._entry(AUG, 450.0, employee=self.colleague)
        self._payslip(self._leave()).write({'state': 'done'})
        batch = self.aug.batch_id
        batch.sudo().write({'state': 'approved'})
        # A settled row is the record of a payment, not a reason to refuse
        # the department's handover.
        self.assertFalse(batch._held_entries())

        run = self._run(AUG)
        run._build_register(preview=True)
        paid = {l.employee_id: l.earnings for l in run.line_ids}
        self.assertNotIn(self.employee, paid)
        self.assertEqual(paid.get(other.employee_id), 450.0)

    def test_the_excel_summary_lists_it_as_paid_on_the_vacation(self):
        """Left out of the register and the bank files, but not out of the
        month's record: the summary lists it, marked, with no transfer."""
        import openpyxl
        other = self._entry(AUG, 450.0, employee=self.colleague)
        payslip = self._payslip(self._leave())
        payslip.write({'state': 'done'})
        run = self._run(AUG)
        run._build_register(preview=True)

        self.assertNotIn(self.employee.id, run._bas_component_totals(),
                         'The journal must not post what the vacation paid.')

        wizard = self.env['ksw.commission.bank.export.wizard'].sudo().create(
            {'run_id': run.id, 'export_mode': 'journal_entry'})
        book = openpyxl.Workbook()
        wizard._make_comm_summary_excel(
            book, run.line_ids, run._vacation_settled_totals())
        sheet = book['Commission Summary']
        headers = [c.value for c in sheet[1]]
        rows = {r[0]: r for r in sheet.iter_rows(min_row=2, values_only=True)}

        paid = rows[self.employee.name]
        self.assertEqual(paid[headers.index('Status')],
                         'Paid on vacation payslip')
        self.assertIn(payslip.number or payslip.name,
                      paid[headers.index('Paid On')])
        self.assertEqual(paid[headers.index('Total Earnings')], 300.0)
        self.assertEqual(paid[headers.index('Bank Transfer Amount')], 0)

        transfer = rows[other.employee_id.name]
        self.assertEqual(transfer[headers.index('Status')], 'Bank transfer')
        self.assertEqual(transfer[headers.index('Bank Transfer Amount')],
                         450.0)

    # ------------------------------------------------------------------
    # The leave's Accounting page
    # ------------------------------------------------------------------
    def test_leave_panel_and_double_payment_warning(self):
        leave = self._leave()
        self.assertTrue(leave.x_commission_entries_applies)
        self.assertEqual(leave.x_commission_entries_total, 1000.0)
        self.assertEqual(leave.x_commission_entries_count, 2)
        self.assertFalse(leave.x_commission_entries_settled)
        self.assertFalse(leave.x_commission_manual_duplicate)

        leave.sudo().write({'x_commission_line_ids': [
            (0, 0, {'name': 'Aug 2031', 'amount': 300.0})]})
        self.assertTrue(leave.x_commission_manual_duplicate)

        self._payslip(leave).write({'state': 'done'})
        leave.invalidate_recordset()
        self.assertTrue(leave.x_commission_entries_settled)
        self.assertEqual(leave.x_commission_entries_total, 1000.0)

    def test_batch_links_only_for_who_can_open_it(self):
        leave = self._leave()
        href = '/odoo/ksw.pay.batch/%d' % self.aug.batch_id.id
        officer = self.env['res.users'].create({
            'name': 'settlement_officer', 'login': 'settlement_officer',
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('KSW_commissions.group_commission_officer').id,
            ])],
        })
        outsider = self.env['res.users'].create({
            'name': 'settlement_outsider', 'login': 'settlement_outsider',
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.assertIn(href, leave.with_user(officer).x_commission_entries_html)
        html = leave.with_user(outsider).x_commission_entries_html
        self.assertNotIn(href, html,
                         'No link to a batch the viewer cannot open.')
        self.assertIn(self.aug.batch_id.name, html)


class TestCommissionLatch(_SettlementCommon):
    """What Accounting reviewed stays on the request.

    Dev leave 47147, Sep 2026: after GM final the GM approved a June batch,
    and the request — read live — started showing a commission its payslip
    never paid. The list is latched when the request first reaches Step 4
    and only the accountant's Refresh, at Step 4, fetches it again.
    """

    def _to_accounting(self, leave):
        leave.sudo().write({'x_annual_approval_state': 'pending_acc'})
        leave.invalidate_recordset()
        return leave

    def test_latched_when_it_reaches_accounting(self):
        leave = self._to_accounting(self._leave())
        self.assertTrue(leave.x_commission_latched_date)
        self.assertEqual(leave.x_commission_entries_total, 1000.0)
        self.assertFalse(leave.x_commission_live_changed)

    def test_a_later_approval_does_not_change_the_request(self):
        leave = self._to_accounting(self._leave())
        self._entry(AUG, 50.0)          # a new, approved August entry
        leave.invalidate_recordset()
        self.assertEqual(leave.x_commission_entries_total, 1000.0,
                         'The request keeps what was latched.')
        self.assertTrue(leave.x_commission_live_changed)
        self.assertEqual(leave.x_commission_live_total, 1050.0)
        self.assertEqual(
            sum(self._com_inputs(self._payslip(leave)).values()), 1000.0,
            'The payslip pays the latch, not the live app.')

    def test_refresh_at_accounting_takes_the_new_figures(self):
        leave = self._to_accounting(self._leave())
        self._entry(AUG, 50.0)
        leave.sudo().action_refresh_commission_entries()
        leave.invalidate_recordset()
        self.assertEqual(leave.x_commission_entries_total, 1050.0)
        self.assertFalse(leave.x_commission_live_changed)

    def test_refresh_is_refused_outside_accounting(self):
        leave = self._to_accounting(self._leave())
        leave.sudo().write({'x_annual_approval_state': 'pending_gm_final'})
        with self.assertRaises(UserError):
            leave.sudo().action_refresh_commission_entries()

    def test_returning_to_accounting_does_not_refresh_by_itself(self):
        leave = self._to_accounting(self._leave())
        leave.sudo().write({'x_annual_approval_state': 'pending_gm_final'})
        self._entry(AUG, 50.0)
        self._to_accounting(leave)
        self.assertEqual(leave.x_commission_entries_total, 1000.0)
        self.assertTrue(leave.x_commission_live_changed)

    def test_an_entry_changed_after_the_latch_is_left_out(self):
        leave = self._to_accounting(self._leave())
        self.aug.write({'amount_override': 350.0})
        leave.invalidate_recordset()
        self.assertEqual(leave.x_commission_entries_total, 1000.0,
                         'The latched figure is kept on the request.')
        self.assertEqual(self._com_inputs(self._payslip(leave)),
                         {'KSW_COM_%d' % self.jul.id: 700.0},
                         'A figure that moved is not paid on the old value, '
                         'and not silently swapped for the new one.')

    def test_past_accounting_without_a_latch_pays_nothing(self):
        leave = self._leave()
        leave.sudo().write({'x_annual_approval_state': 'pending_gm_final'})
        self.assertFalse(self._com_inputs(self._payslip(leave)))
        self.assertEqual(leave.x_commission_entries_total, 0.0)
        self.assertTrue(leave.x_commission_live_changed,
                        'What the app now shows is flagged, not taken.')
        self.assertEqual(leave.x_commission_live_total, 1000.0)


class TestCommissionNeverPaidTwice(_SettlementCommon):
    """One commission, one payment — whichever of the run and the vacation
    (annual, unpaid or EOS) gets there first, the other sees it at once.

    Only Mark Paid commits a month. An exported bank file is not a payment:
    until the month is marked Paid, the vacation pays it and the register
    gives it up (and says the file must be exported again).
    """

    def _approved_run(self, period, earnings):
        return self._run_line(period, self.employee, earnings, 'approved')

    def _mark_paid(self, run):
        run.action_mark_paid()
        return run

    def test_an_exported_but_unpaid_month_is_still_settled(self):
        # KSWCO leave 5218: August exported, not marked Paid.
        run = self._approved_run(JUL, 700.0)
        run.line_ids.write({'x_bank_exported_date': '2031-08-20 08:00:00'})
        self.assertEqual(self._leave()._commission_entries_to_settle(),
                         self.jul | self.aug)

    def test_a_paid_month_is_not_settled(self):
        run = self._approved_run(JUL, 700.0)
        leave = self._leave()
        self._mark_paid(run)
        self.assertEqual(leave._commission_entries_to_settle(), self.aug)

    def test_mark_paid_strips_the_unconfirmed_vacation_payslip(self):
        payslip = self._payslip(self._leave())
        self._mark_paid(self._approved_run(JUL, 700.0))
        self.assertEqual(self._com_inputs(payslip),
                         {'KSW_COM_%d' % self.aug.id: 300.0})
        payslip.write({'state': 'done'})
        self.assertFalse(self.jul.x_vacation_payslip_id)
        self.assertEqual(self.aug.x_vacation_payslip_id, payslip)

    def test_stamping_a_paid_month_is_refused_even_under_sudo(self):
        payslip = self._payslip(self._leave())
        run = self._approved_run(JUL, 700.0)
        run.write({'state': 'paid'})   # behind the drop, as a stale caller
        with self.assertRaises(UserError):
            payslip.write({'state': 'done'})
        with self.assertRaises(UserError):
            self.jul.sudo().with_context(ksw_vacation_settling=True).write(
                {'x_vacation_payslip_id': payslip.id})

    def test_an_entry_paid_on_one_payslip_cannot_be_stamped_on_another(self):
        first = self._payslip(self._leave())
        first.write({'state': 'done'})
        second = self.env['hr.payslip'].create({
            'employee_id': self.employee.id,
            'date_from': SEP, 'date_to': date(2031, 9, 30),
            'version_id': self.version.id,
            'struct_id': self.version.struct_id.id,
        })
        with self.assertRaises(UserError):
            self.aug.sudo().with_context(ksw_vacation_settling=True).write(
                {'x_vacation_payslip_id': second.id})

    def test_vacation_takes_an_exported_line_and_asks_for_a_new_file(self):
        run = self._approved_run(JUL, 700.0)
        run.line_ids.write({'x_bank_exported_date': '2031-08-20 08:00:00'})
        self._payslip(self._leave()).write({'state': 'done'})
        self.assertNotIn(self.employee, run.line_ids.employee_id,
                         'Taken out of the approved register at once.')
        self.assertTrue(run.message_ids.filtered(
            lambda m: 'Export the bank file again' in (m.body or '')))

    def test_confirming_refreshes_an_open_preview_at_once(self):
        other = self._entry(AUG, 450.0, employee=self.colleague)
        run = self._run(AUG)
        run._build_register(preview=True)
        before = {l.employee_id: l.earnings for l in run.line_ids}
        self.assertEqual(before.get(other.employee_id), 450.0)
        payslip = self._payslip(self._leave())
        payslip.write({'state': 'done'})
        after = {l.employee_id: l.earnings for l in run.line_ids}
        self.assertNotIn(self.employee, after)
        payslip.write({'state': 'cancel'})
        self.assertFalse(self.aug.x_vacation_payslip_id)

    def test_the_leave_drops_a_month_marked_paid_since(self):
        leave = self._leave()
        leave._latch_commission_entries()
        self.assertEqual(leave.x_commission_entries_total, 1000.0)
        self._mark_paid(self._approved_run(JUL, 700.0))
        leave.invalidate_recordset()
        self.assertEqual(leave.x_commission_entries_total, 300.0)
        self.assertTrue(leave.x_commission_live_changed)

    # ------------------------------------------------------------------
    # GM final approval: the payslip the approval generates updates the
    # month's register at once — the next bank file does not carry him.
    # ------------------------------------------------------------------
    def _assert_register_updated_at_gm_final(self, leave, create):
        approved = self._approved_run(JUL, 700.0)
        self.env['ksw.pay.run.line'].sudo().create({
            'run_id': approved.id, 'employee_id': self.colleague.id,
            'earnings': 50.0})
        approved.line_ids.write({'x_bank_exported_date': '2031-08-20 08:00:00'})
        other = self._entry(AUG, 450.0, employee=self.colleague)
        preview = self._run(AUG)
        preview._build_register(preview=True)
        self.assertIn(other.employee_id, preview.line_ids.employee_id)

        create(leave)   # what GM final approval calls

        slip = self.env['hr.payslip'].search([
            ('x_leave_id', '=', leave.id), ('state', '=', 'done')])
        self.assertEqual(len(slip), 1, 'Generated and auto-confirmed.')
        self.assertEqual((self.jul | self.aug).x_vacation_payslip_id, slip)
        self.assertNotIn(self.employee, approved.line_ids.employee_id,
                         'Approved register: his line is gone at once.')
        self.assertEqual(
            approved.line_ids.filtered(
                lambda l: l.employee_id == self.colleague).earnings, 50.0)
        self.assertTrue(approved.message_ids.filtered(
            lambda m: 'Export the bank file again' in (m.body or '')),
            'The file made before the approval is flagged as stale.')
        self.assertNotIn(self.employee, preview.line_ids.employee_id,
                         'Open register: rebuilt at once.')
        self.assertIn(other.employee_id, preview.line_ids.employee_id)

    def test_gm_final_vacation_updates_the_register_at_once(self):
        leave = self._leave()
        self._assert_register_updated_at_gm_final(
            leave, lambda l: l._create_vacation_payslip())

    def test_gm_final_eos_updates_the_register_at_once(self):
        if 'is_eos_leave' not in self.env['hr.leave.type']._fields:
            self.skipTest('KSW_eos_leave not installed')
        eos_type = self.env['hr.leave.type'].create({
            'name': 'Settlement EOS (GM final)',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'request_unit': 'day',
            'is_eos_leave': True,
        })
        leave = self._leave(eos_type, date_to=date(2031, 8, 17))
        leave.sudo().write({'x_eos_termination_reason': '84'})
        self._assert_register_updated_at_gm_final(
            leave, lambda l: l._create_eos_payslip())
