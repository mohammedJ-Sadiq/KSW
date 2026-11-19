from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class HrLeave(models.Model):
    _inherit = 'hr.leave'

    # Set when a GM end-of-service decision opened this request, never edited.
    x_employee_release_line_id = fields.Many2one('ksw.employee.release.line', string='Released By',
                                                 readonly=True, copy=False, index=True)

    def write(self, vals):
        res = super().write(vals)
        if vals.get('state') in ('refuse', 'cancel'):
            self.sudo().x_employee_release_line_id._ksw_eos_dropped()
        elif 'state' in vals:
            self.sudo().x_employee_release_line_id.request_id._sync_state()
        return res


class KswEmployeeReleaseLine(models.Model):
    """A GM end of service IS the End of Service request in Time Off, opened
    by the decision itself: its own approval chain and EOS payslip run
    there, and nothing waits for anyone in this app."""
    _inherit = 'ksw.employee.release.line'

    x_eos_leave_ids = fields.One2many('hr.leave', 'x_employee_release_line_id', string='End of Service')
    # Where the request is now, for whoever reads the release: a sudo label,
    # not a link (a department manager may not read that time off).
    x_eos_progress = fields.Char(string='End of Service', compute='_compute_eos_progress', compute_sudo=True)
    status = fields.Selection(selection_add=[('eos', 'End of Service in Time Off')],
                              ondelete={'eos': lambda lines: lines.write({'status': 'terminating'})})

    @api.depends('x_eos_leave_ids.state')
    def _compute_status(self):
        super()._compute_status()
        for line in self.filtered(lambda l: l.status == 'terminating'):
            leaves = line.sudo().x_eos_leave_ids.filtered(lambda l: l.state not in ('refuse', 'cancel'))
            if leaves.filtered(lambda l: l.state == 'validate'):
                line.status = 'terminated'
                line.is_closed = True
            elif leaves:
                line.status = 'eos'

    @api.depends('x_eos_leave_ids.state', 'x_eos_leave_ids.x_annual_approval_state')
    def _compute_eos_progress(self):
        for line in self:
            leave = line.x_eos_leave_ids.sorted('id')[-1:]
            if not leave:
                line.x_eos_progress = False
                continue
            step = leave.x_annual_approval_state
            label = dict(leave._fields['x_annual_approval_state']._description_selection(self.env)).get(step) \
                if step and leave.state not in ('refuse', 'cancel', 'validate') else \
                dict(leave._fields['state']._description_selection(self.env)).get(leave.state)
            line.x_eos_progress = '%s: %s' % (leave.display_name, label or '')

    def _ksw_on_terminate(self, date):
        res = super()._ksw_on_terminate(date)
        self._ksw_create_eos_leave(date)
        return res

    def _ksw_create_eos_leave(self, date):
        leave_type = self.env['hr.leave.type'].sudo().search([('is_eos_leave', '=', True)], limit=1)
        if not leave_type:
            raise UserError(_('No End of Service time off type is configured.'))
        Leave = self.env['hr.leave'].sudo()
        for line in self.sudo():
            leave = Leave.create({
                'employee_id': line.employee_id.id,
                'holiday_status_id': leave_type.id,
                'request_date_from': date, 'request_date_to': date,
                'x_eos_termination_reason': '84',
                'x_employee_release_line_id': line.id,
                'name': _('End of service: GM decision on %s', line.request_id.name),
            })
            line.request_id.message_post(body=Markup(_(
                'End of Service request for <b>%(e)s</b> opened in Time Off (termination date %(d)s): '
                'it continues there.')) % {'e': line.employee_id.name, 'd': date},
                subtype_xmlid='mail.mt_note')
            leave.message_post(body=Markup(_('Opened by the GM\'s end-of-service decision on %s.'))
                               % line.request_id.name, subtype_xmlid='mail.mt_note')

    def _ksw_eos_dropped(self):
        """Refused or cancelled in Time Off: the decision did not stand, the
        employee is back on the GM's list."""
        for line in self.filtered(lambda l: l.decision == 'terminate'):
            if line.x_eos_leave_ids.filtered(lambda l: l.state not in ('refuse', 'cancel')):
                continue
            line.write({'decision': 'pending'})
            line.request_id.message_post(body=Markup(_(
                'The End of Service request for <b>%s</b> was refused or cancelled in Time Off: '
                'he is back with the GM.')) % line.employee_id.name, subtype_xmlid='mail.mt_note')
        self.request_id._sync_state()

    def action_hr_terminate(self):
        """Only reached by a line decided before this module: open its EOS
        request now instead of marking it done here."""
        if not (self.env.su or self.env.user.has_group('KSW_employee_transfer.group_transfer_hr')):
            raise UserError(_('Only HR carries out an end of service.'))
        lines = self.filtered(lambda l: l.status == 'terminating')
        lines._ksw_create_eos_leave(fields.Date.context_today(self))
        lines.request_id._sync_state()
        return True
