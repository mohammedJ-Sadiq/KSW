# -*- coding: utf-8 -*-
import json
import logging

from odoo import fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Rough cost budget in employee-days. Below this the run happens inline so the
# user sees the result immediately; above it we hand off to the background cron
# because the HTTP worker would hit the 120s real-time limit.
_INLINE_BUDGET = 15000


class KswWeekendGrantWizard(models.TransientModel):
    _name = 'ksw.weekend.grant.wizard'
    _inherit = ['ksw.biometric.scope.mixin']
    _description = 'Check / Generate Granted Weekend Days'

    def action_check(self):
        """Dry run: report what a real run would create, change nothing."""
        self.ensure_one()
        employees = self._scoped_employees()

        result = self.env['biometric.attendance.sync']._generate_weekend_records(
            employees, self.date_from, self.date_to, dry_run=True)
        self.result_message = _(
            'Checked %(emp)d employee(s) from %(start)s to %(end)s.\n\n'
            'Missing weekend grants that would be created: %(created)d\n'
            'Stale grants that would be revoked (both adjacent workdays '
            'absent): %(revoked)d\n\n'
            'Nothing has been changed yet. Click "Generate Weekend Grants" '
            'to apply.'
        ) % {
            'emp': len(employees), 'start': self.date_from, 'end': self.date_to,
            'created': result['created'], 'revoked': result['revoked'],
        }
        return self._reopen()

    def action_generate(self):
        """Create the missing weekend grants for the selected scope."""
        self.ensure_one()
        employees = self._scoped_employees()

        if len(employees) * self._day_span() > _INLINE_BUDGET:
            return self._schedule_background(employees)

        result = self.env['biometric.attendance.sync']._generate_weekend_records(
            employees, self.date_from, self.date_to, commit=False)
        _logger.info(
            "Weekend grant wizard: %d employees, %s to %s — %d granted, "
            "%d revoked", len(employees), self.date_from, self.date_to,
            result['created'], result['revoked'])
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Weekend Grants Updated'),
                'message': _(
                    '%(created)d weekend day(s) granted, %(revoked)d stale '
                    'grant(s) revoked for %(emp)d employee(s).'
                ) % {'created': result['created'],
                     'revoked': result['revoked'],
                     'emp': len(employees)},
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

    def _schedule_background(self, employees):
        """Hand a large run to the background cron (see _run_generate_weekend_grants)."""
        Sync = self.env['biometric.attendance.sync']
        cron = self.env.ref(
            'KSW_attendance_leave.ir_cron_ksw_generate_weekend_grants',
            raise_if_not_found=False)
        if not cron:
            raise UserError(_(
                'The background job for weekend grants is missing. Upgrade '
                'the KSW_attendance_leave module and try again.'))

        self.env['ir.config_parameter'].sudo().set_param(
            Sync._WEEKEND_PARAM,
            json.dumps({
                'employee_ids': employees.ids,
                'date_from': fields.Date.to_string(self.date_from),
                'date_to': fields.Date.to_string(self.date_to),
            }))
        cron.sudo().write({'active': True})
        cron.sudo()._trigger()
        _logger.info(
            "Weekend grant wizard: scheduled background run for %d employees, "
            "%s to %s", len(employees), self.date_from, self.date_to)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Scheduled'),
                'message': _(
                    'This range covers %(emp)d employees and is too large to '
                    'run on screen, so it was scheduled in the background. '
                    'Progress is written to the server log.'
                ) % {'emp': len(employees)},
                'type': 'success',
                'sticky': True,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
