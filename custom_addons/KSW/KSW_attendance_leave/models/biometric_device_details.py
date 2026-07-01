# -*- coding: utf-8 -*-
import logging
from datetime import timedelta

from markupsafe import Markup

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)

# The incremental download cron runs every 30 minutes. Three missed cycles
# (90 minutes) tolerates one transient blip without alerting, while still
# catching a device that's actually down well before a multi-hour gap.
_STALE_THRESHOLD_MINUTES = 90


class BiometricDeviceDetailsKSW(models.Model):
    _inherit = 'biometric.device.details'

    x_stale_alert_sent = fields.Boolean(
        default=False,
        help='Set when a stale-sync alert email has been sent for the '
             'current outage; cleared once the device syncs successfully '
             'again so the next outage can re-alert.')

    @api.model
    def cron_check_stale_devices(self):
        """Email base.group_system users when a device stops syncing.

        last_download_time only moves forward on a *successful* connect,
        so a device that's down simply stops advancing it. The download
        cron only logs failures server-side (see _connect_device /
        cron_download_incremental in hr_biometric_attendance) with no
        other visibility, which let a 4-hour Nabia outage go unnoticed on
        2026-06-30 until someone manually compared last_download_time
        across devices.
        """
        threshold = fields.Datetime.now() - timedelta(minutes=_STALE_THRESHOLD_MINUTES)
        devices = self.search([('auto_download', '=', True)])
        for device in devices:
            is_stale = not device.last_download_time or device.last_download_time < threshold
            if is_stale and not device.x_stale_alert_sent:
                device._send_stale_alert()
                device.x_stale_alert_sent = True
            elif not is_stale and device.x_stale_alert_sent:
                device.x_stale_alert_sent = False

    def action_generate_all_absences(self):
        """Override: tell the background cron to process only this device's employees.

        The upstream implementation (biometric.attendance.sync.action_generate_all_absences)
        triggers a cron that searches ALL employees with a biometric_user_id,
        regardless of which device the button was clicked on.  We write the
        current device's ID to an ir.config_parameter first so the cron
        override in biometric_attendance_sync.py can filter to just this
        device's employees.
        """
        self.ensure_one()
        self.env['ir.config_parameter'].sudo().set_param(
            'ksw_attendance_leave.generate_absences_device_id', str(self.id)
        )
        return self.env['biometric.attendance.sync'].action_generate_all_absences()

    def _send_stale_alert(self):
        self.ensure_one()
        recipients = self.env.ref('base.group_system').users.filtered('email')
        if not recipients:
            _logger.warning(
                "Device %s is stale but no base.group_system user has an "
                "email configured to alert.", self.name)
            return

        minutes_str = _('never synced')
        if self.last_download_time:
            minutes = int((fields.Datetime.now() - self.last_download_time).total_seconds() // 60)
            minutes_str = _('%d minutes ago') % minutes

        body = Markup(
            '<p>Biometric device <b>%(name)s</b> (%(ip)s:%(port)s) has not '
            'synced successfully since: <b>%(since)s</b>.</p>'
            '<p>The incremental download cron runs every 30 minutes; this '
            'device has missed several cycles in a row, which usually '
            'means the device is powered off, its network/router is down, '
            'or its IP address changed.</p>'
            '<p>Check device power and network connectivity, then use '
            '<i>Test Connection</i> on the device record. Server log '
            'entries for this device are tagged with its name "%(name)s".</p>'
        ) % {
            'name': self.name,
            'ip': self.device_ip,
            'port': self.port_number,
            'since': minutes_str,
        }

        self.env['mail.mail'].sudo().create({
            'subject': _('Biometric device "%s" sync is stale') % self.name,
            'body_html': body,
            'email_to': ','.join(recipients.mapped('email')),
            'auto_delete': True,
        }).send()
        _logger.warning(
            "Sent stale-sync alert for device %s to %s",
            self.name, ', '.join(recipients.mapped('email')))
