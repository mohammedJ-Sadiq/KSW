"""Commission entries are paid on the vacation payslip, not re-typed.

The deductions twin: a vacation settles the employee's whole position at the
request, and what he is still owed in commissions is already recorded as
``ksw.pay.entry`` rows. The settlement reads them:

  * every month up to and including the departure month, unless that
    month's own pay run was already approved (it went out through the
    register);
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


class TestVacationCommissionSettlement(TransactionCase):

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

    # ------------------------------------------------------------------
    # Which entries
    # ------------------------------------------------------------------
    def test_months_up_to_departure_are_settled(self):
        entries = self._leave()._commission_entries_to_settle()
        self.assertEqual(entries, self.jul | self.aug,
                         'September starts after he left: not the '
                         'settlement\'s business.')

    def test_month_already_paid_by_its_run_is_left_alone(self):
        self._run(JUL).write({'state': 'approved'})
        entries = self._leave()._commission_entries_to_settle()
        self.assertEqual(entries, self.aug)

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
