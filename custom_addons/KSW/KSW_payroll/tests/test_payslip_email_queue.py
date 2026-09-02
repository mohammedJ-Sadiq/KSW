# -*- coding: utf-8 -*-
"""Confirming a payslip queues its email; it never renders a PDF.

Prod incident (KSWCO, 2026-09-02): confirming batch 250 "August 2026
Payslip" (376 slips) reloaded the whole server.  ``done_payslip_run``
confirms every slip in one HTTP request, and ``_send_auto_payslip_email``
called ``mail.template.send_mail(force_send=False)`` per slip —
``force_send`` defers *delivery*, not the attachment, so
``_generate_template_attachments`` rendered the payslip PDF right there.
21 of the 376 August employees have Auto-Email Payslip enabled, at ~3s a
render, which pushed the request past ``limit_time_real = 120``.  Prod
runs ``workers = 0``, so the timeout reloaded the entire server: the BAS
cron died mid-write and the confirmation rolled back in full.

The button now only stamps the queue; ``_cron_send_payslip_emails`` does
the rendering, where ``limit_time_real_cron = -1`` has no ceiling.
"""
from datetime import date
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestPayslipEmailQueue(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.group_user = cls.env.ref('base.group_user')
        cls.group_officer = cls.env.ref('om_hr_payroll.group_hr_payroll_user')

        cls.user_officer = cls.env['res.users'].create({
            'name': 'Payroll Officer',
            'login': 'emailqueue_officer',
            'email': 'emailqueue_officer@queue.test',
            'group_ids': [(6, 0, [cls.group_user.id, cls.group_officer.id])],
        })
        cls.user_plain = cls.env['res.users'].create({
            'name': 'Plain User',
            'login': 'emailqueue_plain',
            'email': 'emailqueue_plain@queue.test',
            'group_ids': [(6, 0, [cls.group_user.id])],
        })

        # Wants the email, and can receive it.
        cls.emp_auto = cls.env['hr.employee'].create({
            'name': 'Auto Email Employee',
            'work_email': 'auto@queue.test',
            'x_auto_send_payslip': True,
        })
        # Wants nothing.
        cls.emp_manual = cls.env['hr.employee'].create({
            'name': 'No Auto Email Employee',
            'work_email': 'manual@queue.test',
            'x_auto_send_payslip': False,
        })
        # Wants the email but has nowhere to send it.
        cls.emp_no_mail = cls.env['hr.employee'].create({
            'name': 'No Work Email Employee',
            'x_auto_send_payslip': True,
        })

    def _new_payslip(self, employee):
        return self.env['hr.payslip'].create({
            'employee_id': employee.id,
            'name': 'Payslip for %s' % employee.name,
            'date_from': date(2026, 8, 1),
            'date_to': date(2026, 8, 31),
        })

    def _confirm(self, slip):
        """Confirm without the real compute — this suite is about the email."""
        with patch.object(type(slip), 'compute_sheet', lambda self: True):
            slip.action_payslip_done()

    # ------------------------------------------------------------------
    # Confirmation queues, and renders nothing
    # ------------------------------------------------------------------

    def test_confirm_queues_the_email(self):
        slip = self._new_payslip(self.emp_auto)
        self._confirm(slip)
        self.assertEqual(slip.state, 'done')
        self.assertEqual(slip.x_email_state, 'queued')

    def test_confirm_does_not_render_a_pdf(self):
        """The incident, in one assertion."""
        slip = self._new_payslip(self.emp_auto)
        report_model = type(self.env['ir.actions.report'])
        with patch.object(report_model, '_render_qweb_pdf') as render:
            self._confirm(slip)
        self.assertFalse(
            render.called,
            'Confirming a payslip must not render its PDF — that is what '
            'timed out the August 2026 batch and reloaded the server.')

    def test_confirm_does_not_create_a_mail(self):
        slip = self._new_payslip(self.emp_auto)
        before = self.env['mail.mail'].search_count([])
        self._confirm(slip)
        self.assertEqual(self.env['mail.mail'].search_count([]), before)

    def test_batch_confirm_renders_nothing(self):
        """The real entry point: hr.payslip.run.done_payslip_run."""
        run = self.env['hr.payslip.run'].create({
            'name': 'August 2026 queue test',
            'date_start': date(2026, 8, 1),
            'date_end': date(2026, 8, 31),
        })
        slips = self.env['hr.payslip']
        for emp in (self.emp_auto, self.emp_manual, self.emp_no_mail):
            slip = self._new_payslip(emp)
            slip.payslip_run_id = run.id
            slips |= slip

        report_model = type(self.env['ir.actions.report'])
        with patch.object(type(slips), 'compute_sheet', lambda self: True), \
                patch.object(report_model, '_render_qweb_pdf') as render:
            run.done_payslip_run()

        self.assertFalse(render.called)
        self.assertEqual(run.state, 'done')
        self.assertEqual(
            slips.filtered(lambda s: s.x_email_state == 'queued'),
            slips.filtered(lambda s: s.employee_id == self.emp_auto))

    # ------------------------------------------------------------------
    # Who gets queued
    # ------------------------------------------------------------------

    def test_employee_without_auto_send_is_not_queued(self):
        slip = self._new_payslip(self.emp_manual)
        self._confirm(slip)
        self.assertFalse(slip.x_email_state)

    def test_employee_without_work_email_is_not_queued(self):
        slip = self._new_payslip(self.emp_no_mail)
        self._confirm(slip)
        self.assertFalse(slip.x_email_state)

    # ------------------------------------------------------------------
    # The cron
    # ------------------------------------------------------------------

    def test_cron_sends_and_stamps_sent(self):
        slip = self._new_payslip(self.emp_auto)
        self._confirm(slip)

        template_model = type(self.env['mail.template'])
        with patch.object(template_model, 'send_mail') as send:
            self.env['hr.payslip']._cron_send_payslip_emails(commit=False)

        self.assertTrue(send.called)
        self.assertEqual(send.call_args[0][0], slip.id)
        self.assertEqual(slip.x_email_state, 'sent')
        self.assertFalse(slip.x_email_error)

    def test_cron_leaves_nothing_behind_to_resend(self):
        """A sent slip is not picked up again on the next run."""
        slip = self._new_payslip(self.emp_auto)
        self._confirm(slip)
        template_model = type(self.env['mail.template'])
        with patch.object(template_model, 'send_mail') as send:
            self.env['hr.payslip']._cron_send_payslip_emails(commit=False)
            self.env['hr.payslip']._cron_send_payslip_emails(commit=False)
        self.assertEqual(send.call_count, 1)

    def test_cron_records_a_failure_and_carries_on(self):
        """One unrenderable slip must not block the queue behind it."""
        bad = self._new_payslip(self.emp_auto)
        self._confirm(bad)
        good = self._new_payslip(self.emp_auto)
        self._confirm(good)

        calls = []

        def _flaky(self, res_id, **kwargs):
            calls.append(res_id)
            if res_id == bad.id:
                raise ValueError('wkhtmltopdf exploded')
            return True

        template_model = type(self.env['mail.template'])
        with patch.object(template_model, 'send_mail', _flaky):
            self.env['hr.payslip']._cron_send_payslip_emails(commit=False)

        self.assertEqual(calls, [bad.id, good.id])
        self.assertEqual(bad.x_email_state, 'failed')
        self.assertIn('wkhtmltopdf exploded', bad.x_email_error)
        self.assertEqual(good.x_email_state, 'sent')

    def test_cron_respects_its_batch_limit(self):
        slips = self.env['hr.payslip']
        for _i in range(3):
            slip = self._new_payslip(self.emp_auto)
            self._confirm(slip)
            slips |= slip

        template_model = type(self.env['mail.template'])
        with patch.object(template_model, 'send_mail') as send:
            self.env['hr.payslip']._cron_send_payslip_emails(
                limit=2, commit=False)

        self.assertEqual(send.call_count, 2)
        self.assertEqual(
            len(slips.filtered(lambda s: s.x_email_state == 'queued')), 1)

    def test_cron_stops_at_its_time_budget(self):
        """The cron is NOT exempt from limit_time_real.

        ``limit_time_real_cron = -1`` reads as "unlimited" and is not —
        ``server.py`` only substitutes it when > 0, so -1 falls through to
        the same 120s ceiling.  A slow render must therefore end the run,
        not the process.
        """
        slips = self.env['hr.payslip']
        for _i in range(4):
            slip = self._new_payslip(self.emp_auto)
            self._confirm(slip)
            slips |= slip

        # Budget of 0s: the first slip goes, then the deadline is past.
        template_model = type(self.env['mail.template'])
        with patch.object(type(self.env['hr.payslip']),
                          '_EMAIL_CRON_SECONDS', 0), \
                patch.object(template_model, 'send_mail') as send:
            self.env['hr.payslip']._cron_send_payslip_emails(commit=False)

        self.assertLess(
            send.call_count, 4,
            'The cron must stop at its time budget, not render the whole queue.')
        self.assertTrue(
            slips.filtered(lambda s: s.x_email_state == 'queued'),
            'Whatever the budget cut off stays queued for the next run.')

    def test_cron_record_is_installed_and_active(self):
        cron = self.env.ref('KSW_payroll.cron_send_payslip_emails')
        self.assertTrue(cron.active)
        self.assertEqual(cron.model_id.model, 'hr.payslip')

    # ------------------------------------------------------------------
    # Re-send
    # ------------------------------------------------------------------

    def test_officer_can_requeue_a_sent_payslip(self):
        slip = self._new_payslip(self.emp_auto)
        self._confirm(slip)
        slip.write({'x_email_state': 'sent'})
        slip.with_user(self.user_officer).action_requeue_payslip_email()
        self.assertEqual(slip.x_email_state, 'queued')

    def test_requeue_clears_a_previous_error(self):
        slip = self._new_payslip(self.emp_auto)
        self._confirm(slip)
        slip.write({'x_email_state': 'failed', 'x_email_error': 'boom'})
        slip.with_user(self.user_officer).action_requeue_payslip_email()
        self.assertEqual(slip.x_email_state, 'queued')
        self.assertFalse(slip.x_email_error)

    def test_non_payroll_user_cannot_requeue(self):
        slip = self._new_payslip(self.emp_auto)
        self._confirm(slip)
        with self.assertRaises(UserError):
            slip.with_user(self.user_plain).action_requeue_payslip_email()

    def test_cannot_requeue_a_draft_payslip(self):
        slip = self._new_payslip(self.emp_auto)
        with self.assertRaises(UserError):
            slip.with_user(self.user_officer).action_requeue_payslip_email()

    def test_cannot_requeue_without_a_work_email(self):
        slip = self._new_payslip(self.emp_no_mail)
        self._confirm(slip)
        with self.assertRaises(UserError):
            slip.with_user(self.user_officer).action_requeue_payslip_email()
