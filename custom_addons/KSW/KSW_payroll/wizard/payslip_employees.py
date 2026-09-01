from odoo import api, fields, models, _


class HrPayslipEmployees(models.TransientModel):
    """Override the standard 'Generate Payslips' wizard to:

    1. Pre-check each employee for blocking conditions before creating a
       payslip (no active contract, unconfirmed return from leave).
    2. Skip problematic employees instead of raising a hard error for the
       whole batch.
    3. Record the skipped employees + reasons on the batch
       (``ksw.payslip.run.skip.line``) for later review.
    4. Return a sticky warning notification if any employees were skipped.
    """
    _inherit = 'hr.payslip.employees'

    employee_ids = fields.Many2many(
        'hr.employee',
        'hr_employee_group_rel', 'payslip_id', 'employee_id',
        domain=lambda self: []
               if self.env.user.has_group('base.group_system')
               else [('x_exclude_from_payroll', '=', False)],
    )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @api.model
    def _check_employee_for_batch(self, employee, from_date, to_date):
        """Return a blocking reason string if the employee must be skipped,
        or an empty string if the employee is eligible for payslip generation.

        Checks (in order):
        1. No active contract / version for the period.
        2. A leave (annual or unpaid) whose return the direct manager has
           not confirmed — the system does not know which days were worked.
        3. A confirmed payslip already covers the period — the slip would
           be created but could never be confirmed
           (``hr.payslip._check_duplicate_done_period``).  Corrections go
           through "Issue Revision" on the confirmed payslip instead.
        """
        HrPayslip = self.env['hr.payslip']

        # -- Contract check --------------------------------------------------
        slip_data = HrPayslip.onchange_employee_id(
            from_date, to_date, employee.id, contract_id=False)
        version_id = slip_data.get('value', {}).get('version_id')
        struct_id = slip_data.get('value', {}).get('struct_id')
        if not version_id or not struct_id:
            return _('No active contract / salary structure for this period')

        # -- Vacation-return check -------------------------------------------
        unresolved = HrPayslip._get_unresolved_vacation_leaves(
            employee.id, to_date)
        if unresolved:
            details = ', '.join(
                '%s (%s → %s)' % (
                    l.holiday_status_id.name,
                    l.request_date_from,
                    l.request_date_to,
                )
                for l in unresolved
            )
            manager = employee.sudo().leave_manager_id
            return _(
                'Return not confirmed — waiting on %(manager)s to press '
                '"Confirm Return" on: %(details)s',
                manager=(manager.name if manager
                         else _('the Time Off manager')),
                details=details,
            )

        # -- Already-confirmed period ----------------------------------------
        # Mirrors hr.payslip._check_duplicate_done_period: a confirmed
        # vacation payslip whose return the direct manager has confirmed
        # does not block the rest of the month.
        confirmed = HrPayslip.sudo().search([
            ('employee_id', '=', employee.id),
            ('state', '=', 'done'),
            ('date_from', '<=', to_date),
            ('date_to', '>=', from_date),
        ])
        blocking = confirmed.filtered(
            lambda s: not s._is_settled_vacation_payslip())
        if blocking:
            return _(
                'A confirmed payslip already exists for this period (%s). '
                'Use "Issue Revision" on it if a correction is needed.'
            ) % ', '.join(
                s.number or s.name or str(s.id) for s in blocking)

        return ''

    @api.model
    def _check_employee_warnings(self, employee, from_date, to_date):
        """Return a warning string for an employee that WILL be processed
        but whose figures need a human look, or '' when there is nothing
        to flag.

        Unlike _check_employee_for_batch this never skips anyone — the
        payslip is still generated. It exists so payroll can see *why* a net
        collapsed, instead of discovering it on the bank file.
        """
        if not employee.sudo().x_is_attendance_sheet:
            return ''

        sheet = self.env['ksw.attendance.sheet'].sudo().search([
            ('employee_id', '=', employee.id),
            ('month', '=', str(from_date.month)),
            ('year', '=', from_date.year),
        ], limit=1)
        if sheet and sheet.state == 'confirmed':
            return ''

        manager = sheet.manager_id.name if sheet else ''
        if not sheet:
            return _(
                'No attendance sheet exists for this month — the payslip '
                'was computed as zero attendance.')
        return _(
            'Attendance sheet not sent to payroll%(by)s — the payslip was '
            'computed as zero attendance.',
            by=(' (%s)' % manager) if manager else '',
        )

    # ------------------------------------------------------------------
    # Override generate action
    # ------------------------------------------------------------------

    def compute_sheet(self):
        """Generate payslips for eligible employees, skip the rest and
        log the skips on the payslip batch."""
        payslip_model = self.env['hr.payslip']
        [data] = self.read()
        active_id = self.env.context.get('active_id')
        if not active_id:
            return super().compute_sheet()

        [run_data] = self.env['hr.payslip.run'].browse(active_id).read(
            ['date_start', 'date_end', 'credit_note'])
        from_date = run_data.get('date_start')
        to_date = run_data.get('date_end')

        if not data['employee_ids']:
            from odoo.exceptions import UserError
            raise UserError(_("You must select employee(s) to generate payslip(s)."))

        # Clear any previous skip log for this batch
        run = self.env['hr.payslip.run'].browse(active_id)
        run.x_skip_line_ids.unlink()

        payslips = payslip_model
        skipped = []   # list of (employee, reason)
        warned = []    # list of (employee, reason) — processed anyway

        is_admin = self.env.user.has_group('base.group_system')
        for employee in self.env['hr.employee'].browse(data['employee_ids']):
            if employee.x_exclude_from_payroll and not is_admin:
                continue
            reason = self._check_employee_for_batch(employee, from_date, to_date)
            if reason:
                skipped.append((employee, reason))
                continue

            warning = self._check_employee_warnings(
                employee, from_date, to_date)
            if warning:
                warned.append((employee, warning))

            # Employee is eligible — create the payslip
            slip_data = payslip_model.onchange_employee_id(
                from_date, to_date, employee.id, contract_id=False)
            res = {
                'employee_id': employee.id,
                'name': slip_data['value'].get('name'),
                'struct_id': slip_data['value'].get('struct_id'),
                'version_id': slip_data['value'].get('version_id'),
                'payslip_run_id': active_id,
                'input_line_ids': [
                    (0, 0, x) for x in slip_data['value'].get('input_line_ids', [])],
                'worked_days_line_ids': [
                    (0, 0, x) for x in slip_data['value'].get('worked_days_line_ids', [])],
                'date_from': from_date,
                'date_to': to_date,
                'credit_note': run_data.get('credit_note'),
                'company_id': employee.company_id.id,
            }
            payslips += payslip_model.create(res)

        # Compute salary sheets for eligible payslips
        if payslips:
            payslips.compute_sheet()

        # Persist skip + warning log on the batch
        log_vals = [
            {
                'run_id': active_id,
                'employee_id': emp.id,
                'reason': rsn,
                'line_type': line_type,
            }
            for entries, line_type in ((skipped, 'skipped'),
                                       (warned, 'warning'))
            for emp, rsn in entries
        ]
        if log_vals:
            self.env['ksw.payslip.run.skip.line'].create(log_vals)

        if skipped or warned:
            # Return a sticky warning notification so the user notices
            parts = []
            if skipped:
                parts.append(_(
                    'Skipped (no payslip): %s',
                    ', '.join(e.name for e, _r in skipped)))
            if warned:
                parts.append(_(
                    'Paid as zero attendance (sheet not confirmed): %s',
                    ', '.join(e.name for e, _r in warned)))
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _(
                        'Batch Generated — %(skipped)d Skipped, '
                        '%(warned)d Need Review',
                        skipped=len(skipped), warned=len(warned),
                    ),
                    'message': '%s\n%s' % (
                        '\n'.join(parts),
                        _('Open the "Skipped Employees" tab on the batch '
                          'for details.'),
                    ),
                    'type': 'warning',
                    'sticky': True,
                    'next': {'type': 'ir.actions.act_window_close'},
                },
            }

        return {'type': 'ir.actions.act_window_close'}

