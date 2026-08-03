# -*- coding: utf-8 -*-
import logging

from odoo import fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# The device connection can't be handed to a background job mid-run, so this
# wizard always works inline. Past this many employee-days the HTTP worker
# would hit the 120s limit, so we ask the user to narrow instead of failing
# halfway through.
_MAX_SCOPE = 15000


class KswAttendanceDownloadWizard(models.TransientModel):
    _name = 'ksw.attendance.download.wizard'
    _inherit = ['ksw.biometric.scope.mixin']
    _description = 'Download Attendance for a Specific Period'

    generate_absences = fields.Boolean(
        string='Generate absences', default=True,
        help='Create the absence records for scheduled workdays with no punch '
             'in this period. These are Odoo-side rows, so a re-download does '
             'not restore them on its own.')
    generate_weekend_grants = fields.Boolean(
        string='Generate weekend grants', default=True,
        help='Re-create the granted Friday/Saturday records for this period. '
             'These exist only in Odoo and are always lost when attendance is '
             'cleared and re-downloaded.')

    def action_download(self):
        """Pull this device's punches for the chosen period and employees."""
        self.ensure_one()
        employees = self._scoped_employees()

        span = self._day_span()
        if len(employees) * span > _MAX_SCOPE:
            raise UserError(_(
                'This selection covers %(emp)d employees over %(days)d days, '
                'which is too large to download in one request. Narrow the '
                'period or pick specific employees.'
            ) % {'emp': len(employees), 'days': span})

        Sync = self.env['biometric.attendance.sync']
        downloaded = Sync.download_attendance_range(
            self.device_id, self.date_from, self.date_to, employees)

        lines = [
            _('Device: %(device)s\n'
              'Period: %(start)s to %(end)s\n'
              'Employees: %(emp)d\n\n'
              'Punches downloaded: %(created)d day(s) created, '
              '%(updated)d updated.'
              ) % {
                'device': self.device_id.name,
                'start': self.date_from, 'end': self.date_to,
                'emp': len(employees),
                'created': downloaded['created'],
                'updated': downloaded['updated'],
            },
        ]

        if self.generate_absences:
            absences = Sync._generate_absences_date_range(
                employees, self.date_from, self.date_to, commit=False)
            lines.append(_('Absence records created: %d') % absences)

        if self.generate_weekend_grants:
            weekend = Sync._generate_weekend_records(
                employees, self.date_from, self.date_to, commit=False)
            lines.append(_(
                'Weekend grants: %(created)d granted, %(revoked)d revoked'
            ) % {'created': weekend['created'],
                 'revoked': weekend['revoked']})

        if not downloaded['created'] and not downloaded['updated']:
            lines.append(_(
                '\nNo punches came back for this period. Check that the '
                'period is right and that the device still holds logs that '
                'far back — "Clear Attendance" wipes them on the device too.'))

        self.result_message = '\n'.join(lines)
        _logger.info(
            "Specific attendance download: device %s, %s to %s, %d employees "
            "— %d created, %d updated",
            self.device_id.name, self.date_from, self.date_to, len(employees),
            downloaded['created'], downloaded['updated'])
        return self._reopen()
