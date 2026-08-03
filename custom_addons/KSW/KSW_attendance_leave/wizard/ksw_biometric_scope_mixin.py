# -*- coding: utf-8 -*-
"""Shared "which employees, which days" scope for the biometric repair wizards.

Both the weekend-grant wizard and the specific-attendance download wizard exist
for the same reason: the device-level buttons are all-or-nothing, so repairing
one month for one employee means re-processing every employee and every year.
This mixin holds the narrowing controls they have in common.
"""
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class KswBiometricScopeMixin(models.AbstractModel):
    _name = 'ksw.biometric.scope.mixin'
    _description = 'Biometric Device / Period / Employee Scope'

    device_id = fields.Many2one(
        'biometric.device.details', string='Device', required=True)
    date_from = fields.Date(required=True, string='From')
    date_to = fields.Date(required=True, string='To')
    employee_ids = fields.Many2many(
        'hr.employee', string='Employees',
        help='Leave empty to process every biometric employee on the device.')
    include_unassigned = fields.Boolean(
        string='Include employees with no device',
        help='Also process employees that have a biometric ID but no device '
             'assigned. Those employees are skipped by the device-level '
             'buttons, so they are never processed at all.')
    employee_count = fields.Integer(
        compute='_compute_employee_count', string='Employees in scope')
    result_message = fields.Text(readonly=True)

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        if self.env.context.get('active_model') == 'biometric.device.details':
            values['device_id'] = self.env.context.get('active_id')
        today = fields.Date.context_today(self)
        values.setdefault('date_to', today - timedelta(days=1))
        values.setdefault('date_from', today.replace(day=1))
        return values

    @api.depends('device_id', 'employee_ids', 'include_unassigned')
    def _compute_employee_count(self):
        for wiz in self:
            wiz.employee_count = len(wiz._target_employees())

    @api.constrains('date_from', 'date_to')
    def _check_dates(self):
        for wiz in self:
            if wiz.date_from > wiz.date_to:
                raise UserError(_('"From" must be on or before "To".'))

    def _target_employees(self):
        """Employees this run covers, honouring the explicit selection."""
        self.ensure_one()
        if not self.device_id:
            return self.env['hr.employee']
        if self.employee_ids:
            return self.employee_ids
        domain = [('biometric_user_id', '!=', False)]
        if self.include_unassigned:
            domain += ['|', ('device_id', '=', self.device_id.id),
                       ('device_id', '=', False)]
        else:
            domain += [('device_id', '=', self.device_id.id)]
        return self.env['hr.employee'].search(domain)

    def _scoped_employees(self):
        """Target employees, refusing an empty scope."""
        employees = self._target_employees()
        if not employees:
            raise UserError(_('No biometric employees match this selection.'))
        return employees

    def _day_span(self):
        return (self.date_to - self.date_from).days + 1

    def _reopen(self):
        """Keep the dialog open so the user can read the result."""
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
            'context': self.env.context,
        }
