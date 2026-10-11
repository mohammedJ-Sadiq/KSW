# -*- coding: utf-8 -*-
"""A payslip sent to the bank in a text file is frozen; changes wait.

Exporting a bank TEXT file stamps the draft payslips it carries. A change
to a frozen payslip is not applied and not lost: a dry run works out what
it would do, it is kept as a pending change and the Payroll Managers are
notified. A manager applies it (unfreeze + recompute) or dismisses it.
Exporting again with changes pending must be acknowledged. Mark As Done
confirms the frozen figures and closes what was still pending.
"""
from datetime import date

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

from odoo.addons.KSW_payroll.models.hr_payslip_run import (
    HrPayslipRun as RunBase)


class TestPayslipBankFreeze(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Bank Freeze Calendar', 'tz': 'Asia/Riyadh'})
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Bank Freeze Employee',
            'resource_calendar_id': cls.calendar.id,
        })
        cls.version = cls.employee.current_version_id
        cls.version.write({
            'date_version': date(2025, 1, 1),
            'contract_date_start': date(2025, 1, 1),
            'resource_calendar_id': cls.calendar.id,
            'wage': 2000.0, 'hra': 600.0, 'other_allowance': 600.0,
            'travel_allowance': 0.0, 'mobile_allowance': 0.0, 'da': 0.0,
            'meal_allowance': 0.0, 'medical_allowance': 0.0,
            'struct_id': cls.env.ref('om_hr_payroll.structure_base').id,
        })
        cls.employee._compute_current_version_id()
        partner = cls.env.company.partner_id
        cls.bank = cls.env['res.partner.bank'].create({
            'acc_number': 'FREEZE-WPS', 'partner_id': partner.id,
            'x_file_type': 'wps'})
        cls.employee.x_salary_bank_account_id = cls.bank

        def _mkuser(login, *groups):
            return cls.env['res.users'].create({
                'name': login, 'login': login, 'email': login + '@f.test',
                'group_ids': [(6, 0, [cls.env.ref(g).id for g in (
                    'base.group_user',) + groups])]})
        # Computing a payslip reads the contract wage (HR Administrator).
        cls.manager = _mkuser('freeze_mgr', 'hr.group_hr_manager',
                              'om_hr_payroll.group_hr_payroll_manager')
        cls.officer = _mkuser('freeze_off', 'om_hr_payroll.group_hr_payroll_user')

    def setUp(self):
        super().setUp()
        self.run = self.env['hr.payslip.run'].create({
            'name': 'Bank Freeze Batch',
            'date_start': date(2026, 7, 1), 'date_end': date(2026, 7, 31),
        })
        self.slip = self.env['hr.payslip'].create({
            'employee_id': self.employee.id,
            'name': 'Bank freeze slip',
            'date_from': date(2026, 7, 1), 'date_to': date(2026, 7, 31),
            'struct_id': self.env.ref('om_hr_payroll.structure_base').id,
            'version_id': self.version.id,
            'payslip_run_id': self.run.id,
        })
        self.slip.compute_sheet()
        self.paid = self.slip._ksw_net_amount()

    def _wizard(self, mode='specific_excel', **extra):
        vals = {'payslip_run_id': self.run.id, 'export_mode': mode,
                'bank_account_id': self.bank.id}
        vals.update(extra)
        return self.env['ksw.bank.file.export.wizard'].create(vals)

    def _freeze(self):
        """What a text export does once its file exists."""
        wizard = self._wizard('specific_txt')
        self.run._ksw_stamp_bank_export(wizard._exported_slips())
        self.assertTrue(self.slip.x_bank_frozen)

    def _late_change(self):
        self.env['hr.payslip.input'].create({
            'name': 'Arrived after export', 'payslip_id': self.slip.id,
            'code': 'KSW_DED_9999', 'amount': 100.0,
            'version_id': self.version.id,
        })

    def _pending(self):
        return self.run.x_pending_change_ids.filtered(
            lambda p: p.state == 'pending')

    def test_only_a_text_export_freezes(self):
        self._wizard('specific_excel').action_export()
        self.assertFalse(self.slip.x_bank_exported_date,
                         'an Excel export is a review copy')
        self.assertEqual(self._wizard('specific_txt')._exported_slips(),
                         self.slip)
        self.assertEqual(self._wizard('all_txt')._exported_slips(),
                         self.slip)
        self.assertFalse(self._wizard('journal_entry')._exported_slips())
        self._freeze()
        self.assertEqual(self.run.x_frozen_slip_count, 1)

    def test_change_to_a_frozen_payslip_waits(self):
        self._freeze()
        self._late_change()
        result = self.slip.compute_sheet()
        self.assertEqual(result['tag'], 'display_notification')
        self.assertEqual(self.slip._ksw_net_amount(), self.paid,
                         'the frozen figures stay as sent')
        pending = self._pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending.net_current, self.paid)
        self.assertEqual(pending.net_after, self.paid - 100.0)
        self.assertIn('NET', pending.diff_text)
        self.assertTrue(self.slip.x_has_pending_change)

    def test_managers_are_notified_once_per_new_effect(self):
        self._freeze()
        partner = self.manager.partner_id

        def notified():
            return self.run.message_ids.filtered(
                lambda m: partner in m.partner_ids)
        self._late_change()
        self.slip.compute_sheet()
        self.assertEqual(len(notified()), 1)
        self.slip.compute_sheet()   # same effect again: no new message
        self.assertEqual(len(notified()), 1)
        self.assertEqual(len(self._pending()), 1)
        self.assertEqual(len(self._pending().trigger_log.splitlines()), 2)
        self._late_change()          # a different effect: tell them again
        self.slip.compute_sheet()
        self.assertEqual(len(notified()), 2)
        self.assertEqual(self._pending().net_after, self.paid - 200.0)

    def test_nothing_to_change_queues_nothing(self):
        self._freeze()
        self.slip.compute_sheet()
        self.assertFalse(self._pending())

    def test_apply_unfreezes_and_applies(self):
        self._freeze()
        self._late_change()
        self.slip.compute_sheet()
        with self.assertRaises(UserError):
            self._pending().with_user(self.officer).action_apply()
        self.run.with_user(self.manager).action_apply_pending_changes()
        self.assertFalse(self.slip.x_bank_frozen)
        self.assertEqual(self.slip._ksw_net_amount(), self.paid - 100.0)
        change = self.run.x_pending_change_ids
        self.assertEqual(change.state, 'applied')
        self.assertEqual(change.net_applied, self.paid - 100.0)
        self._freeze()   # the next text export freezes it again

    def test_dismiss_keeps_the_payslip_as_sent(self):
        self._freeze()
        self._late_change()
        self.slip.compute_sheet()
        self._pending().with_user(self.manager).action_dismiss()
        self.assertEqual(self.run.x_pending_change_ids.state, 'dismissed')
        self.assertTrue(self.slip.x_bank_frozen)
        self.assertEqual(self.slip._ksw_net_amount(), self.paid)

    def test_export_with_pending_changes_must_be_acknowledged(self):
        self._freeze()
        self._late_change()
        self.slip.compute_sheet()
        wizard = self._wizard('specific_excel')
        self.assertEqual(wizard.pending_change_count, 1)
        self.assertIn(self.employee.name, wizard.pending_change_summary)
        with self.assertRaises(UserError):
            wizard.action_export()
        self._wizard('specific_excel', confirm_pending=True).action_export()

    def test_mark_as_done_confirms_as_sent_and_closes_pending(self):
        self._freeze()
        self._late_change()
        self.slip.compute_sheet()
        self.run.action_open_done_wizard()
        self.assertEqual(self.slip.state, 'done')
        self.assertEqual(self.slip._ksw_net_amount(), self.paid)
        self.assertEqual(self.run.x_pending_change_ids.state, 'expired')

    def test_frozen_payslip_cannot_be_deleted(self):
        self._freeze()
        with self.assertRaises(UserError):
            self.slip.unlink()
        with self.assertRaises(UserError):
            self.run.action_unlink_with_payslips()
        self.assertTrue(self.slip.exists())

    def test_superseded_frozen_slip_is_kept_and_warned_once(self):
        self._freeze()
        self.patch(RunBase, '_ksw_superseded_slip_reasons',
                   lambda run, slips: {s: 'Vacation settles July.'
                                       for s in slips})
        self.run._ksw_drop_superseded_slips()
        self.run._ksw_drop_superseded_slips()
        self.assertEqual(self.slip.state, 'draft')
        self.assertEqual(self.slip.payslip_run_id, self.run)
        warnings = self.run.x_skip_line_ids.filtered(
            lambda l: l.line_type == 'warning')
        self.assertEqual(len(warnings), 1)
