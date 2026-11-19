from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.fields import Domain
from odoo.exceptions import UserError

HR_GROUP = 'KSW_employee_transfer.group_transfer_hr'
GM_GROUP = 'KSW_employee_transfer.group_transfer_gm'
# Set by the route only, never by a form or an RPC write.
ROUTE_FIELDS = {
    'state', 'with_vehicles', 'version_id', 'origin', 'requested_by_id', 'from_department_id',
    'applied_date', 'submitted_date', 'hr_user_id', 'hr_date', 'gm_user_id', 'gm_date',
    'closed_by_id', 'closed_date', 'from_manager_id', 'from_leave_manager_id',
    'from_attendance_manager_id', 'from_hr_responsible_id', 'refuse_reason',
    'release_line_id', 'addition_line_id', 'employee_id', 'to_department_id',
}


class KswEmployeeTransfer(models.Model):
    """The execution of a GM decision: a released employee given to another
    department's request. HR confirms who he answers to there, and the move
    applies on its date once he no longer answers for vehicles. Nobody raises
    a transfer by hand: a department releases or requests, the GM matches."""
    _name = 'ksw.employee.transfer'
    _description = 'Employee Transfer'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(default='New', copy=False, readonly=True)
    employee_id = fields.Many2one('hr.employee', string='Employee', required=True, tracking=True,
                                  index=True, readonly=True)
    # What it was when decided: the employee's department moves on after.
    from_department_id = fields.Many2one('hr.department', string='From Department', readonly=True)
    to_department_id = fields.Many2one('hr.department', string='To Department', required=True,
                                       tracking=True, readonly=True)
    effective_date = fields.Date(required=True, default=fields.Date.context_today, tracking=True)
    reason = fields.Text(required=True)
    state = fields.Selection([
        ('hr', 'With HR'),
        ('approved', 'Waiting for Date'),
        ('applied', 'Applied'),
        ('refused', 'Returned to the GM'),
        ('cancelled', 'Cancelled'),
    ], default='hr', required=True, tracking=True, copy=False, index=True)
    # The GM decision this transfer carries out.
    release_line_id = fields.Many2one('ksw.employee.release.line', string='Released By', readonly=True,
                                      copy=False, index=True)
    addition_line_id = fields.Many2one('ksw.employee.addition.line', string='For Request', readonly=True,
                                       copy=False, index=True)
    requested_by_id = fields.Many2one('res.users', default=lambda self: self.env.user, readonly=True)
    refuse_reason = fields.Text(string='Why Returned', readonly=True, copy=False)
    # HR only: a contract version is not readable by a department manager.
    version_id = fields.Many2one('hr.version', string='Contract Version', readonly=True, copy=False,
                                 groups='hr.group_hr_user')
    applied_date = fields.Datetime(readonly=True, copy=False)
    # Who did each step, and when. The chatter has it too, but not to filter
    # on or to read at a glance.
    submitted_date = fields.Datetime(string='Submitted On', readonly=True, copy=False)
    gm_user_id = fields.Many2one('res.users', string='GM Decided By', readonly=True, copy=False)
    gm_date = fields.Datetime(string='GM Decided On', readonly=True, copy=False)
    hr_user_id = fields.Many2one('res.users', string='HR Confirmed By', readonly=True, copy=False)
    hr_date = fields.Datetime(string='HR Confirmed On', readonly=True, copy=False)
    closed_by_id = fields.Many2one('res.users', string='Returned / Cancelled By', readonly=True, copy=False)
    closed_date = fields.Datetime(string='Returned / Cancelled On', readonly=True, copy=False)
    # What the move replaced, stamped when applied: afterwards the employee
    # only shows the new values.
    from_manager_id = fields.Many2one('hr.employee', string='Previous Manager', readonly=True, copy=False)
    from_leave_manager_id = fields.Many2one('res.users', string='Previous Time Off Approver',
                                            readonly=True, copy=False)
    from_attendance_manager_id = fields.Many2one('res.users', string='Previous Attendance Approver',
                                                 readonly=True, copy=False)
    from_hr_responsible_id = fields.Many2one('res.users', string='Previous HR Responsible',
                                             readonly=True, copy=False)
    # Set when the employee moves together with his vehicles (a vehicle-with-
    # driver transfer): his custody goes with him, so it does not block.
    with_vehicles = fields.Boolean(readonly=True, copy=False)
    origin = fields.Char(readonly=True, copy=False)

    # Who he answers to after the move, applied with it. Proposed from the new
    # department, confirmed by HR: most departments carry no manager, so the
    # department alone cannot decide it.
    to_manager_id = fields.Many2one('hr.employee', string='New Manager', tracking=True,
                                    compute='_compute_to_manager_id', store=True, readonly=False)
    to_leave_manager_id = fields.Many2one('res.users', string='New Time Off Approver', tracking=True,
                                          domain="[('share', '=', False)]",
                                          compute='_compute_to_approvers', store=True, readonly=False)
    to_attendance_manager_id = fields.Many2one('res.users', string='New Attendance Approver', tracking=True,
                                               domain="[('share', '=', False)]",
                                               compute='_compute_to_approvers', store=True, readonly=False)
    to_hr_responsible_id = fields.Many2one('res.users', string='New HR Responsible', tracking=True,
                                           domain=lambda self: [('share', '=', False), (
                                               'all_group_ids', 'in', self.env.ref('hr.group_hr_user').ids)],
                                           compute='_compute_to_hr_responsible_id', store=True, readonly=False)

    custody_vehicle_ids = fields.Many2many('ksw.fleet.vehicle', compute='_compute_checks',
                                           string='Vehicles in His Custody')
    warning_text = fields.Text(compute='_compute_checks', string='Before You Confirm')
    # Home ▸ Waiting for you, and the "Waiting for Me" filter.
    x_is_pending_my_action = fields.Boolean(compute='_compute_is_pending_my_action',
                                            search='_search_is_pending_my_action', string='Waiting for Me')
    x_can_hr = fields.Boolean(compute='_compute_permissions')
    x_can_gm = fields.Boolean(compute='_compute_permissions')

    @api.depends_context('uid')
    @api.depends('state')
    def _compute_permissions(self):
        is_hr = self.env.su or self.env.user.has_group(HR_GROUP)
        is_gm = self.env.su or self.env.user.has_group(GM_GROUP)
        for transfer in self:
            transfer.x_can_hr = transfer.state == 'hr' and is_hr
            transfer.x_can_gm = transfer.state in ('hr', 'approved') and is_gm

    @api.depends('employee_id', 'to_department_id')
    def _compute_to_manager_id(self):
        for transfer in self:
            employee = transfer.employee_id.sudo()
            department = transfer.to_department_id.sudo()
            # The first manager up the new department's tree who is not himself.
            while department and (not department.manager_id or department.manager_id == employee):
                department = department.parent_id
            transfer.to_manager_id = department.manager_id

    @api.depends('to_manager_id')
    def _compute_to_approvers(self):
        for transfer in self:
            user = transfer.to_manager_id.sudo().user_id
            transfer.to_leave_manager_id = user
            transfer.to_attendance_manager_id = user

    @api.depends('employee_id', 'to_department_id')
    def _compute_to_hr_responsible_id(self):
        """The HR responsible most of the new department already has. The
        default (whoever created the employee: admin, OdooBot) is no choice."""
        not_chosen = (self.env.ref('base.user_root') | self.env.ref('base.user_admin')).ids
        Version = self.env['hr.version'].sudo()
        for transfer in self:
            responsible = self.env['res.users']
            if transfer.to_department_id:
                # Current versions only: one per colleague.
                rows = Version._read_group(
                    [('id', 'in', self.env['hr.employee'].sudo().search([
                        ('department_id', '=', transfer.to_department_id.id),
                        ('id', '!=', transfer.employee_id.id)]).current_version_id.ids),
                     ('hr_responsible_id', 'not in', not_chosen)],
                    ['hr_responsible_id'], ['__count'])
                if rows:
                    responsible = max(rows, key=lambda r: r[1])[0]
            transfer.to_hr_responsible_id = responsible

    @api.depends('employee_id', 'effective_date', 'with_vehicles')
    def _compute_checks(self):
        Vehicle = self.env['ksw.fleet.vehicle'].sudo()
        for transfer in self:
            employee = transfer.employee_id.sudo()
            transfer.custody_vehicle_ids = Vehicle.search([('x_custodian_id', '=', employee.id)]) \
                if employee else Vehicle
            transfer.warning_text = '\n'.join(transfer._warnings()) or False

    def _warnings(self):
        """What the move lands in the middle of. Shown, never blocking: HR
        decides. Payroll and attendance are other modules, read if present."""
        self.ensure_one()
        employee = self.employee_id.sudo()
        warnings = []
        if not employee:
            return warnings
        if 'hr.payslip' in self.env:
            slips = self.env['hr.payslip'].sudo().search([
                ('employee_id', '=', employee.id), ('state', 'in', ('draft', 'verify'))])
            if slips:
                warnings.append(_('Open payslips: %s.', ', '.join(slips.mapped('name'))))
        if 'ksw.attendance.sheet' in self.env:
            sheets = self.env['ksw.attendance.sheet'].sudo().search([
                ('employee_id', '=', employee.id), ('state', '=', 'draft')])
            if sheets:
                warnings.append(_('Unconfirmed attendance sheets: %s.', ', '.join(sheets.mapped('display_name'))))
        managed = self.env['hr.department'].sudo().search([('manager_id', '=', employee.id)])
        if managed:
            warnings.append(_('He manages %s: choose its new manager.', ', '.join(managed.mapped('name'))))
        reports = self.env['hr.employee'].sudo().search_count([('parent_id', '=', employee.id)])
        if reports:
            warnings.append(_('%s employees report to him and keep him as their manager.', reports))
        if self.custody_vehicle_ids and not self.with_vehicles and self.state in ('hr', 'approved'):
            warnings.append(_('He still answers for %s: the move waits until they are handed over '
                              '(as his department decided on the release).',
                              ', '.join(self.custody_vehicle_ids.mapped('name'))))
        return warnings

    @api.model
    def _pending_domain(self):
        if self.env.su or self.env.user.has_group(HR_GROUP):
            return Domain('state', '=', 'hr')
        return Domain.FALSE

    @api.depends_context('uid')
    def _compute_is_pending_my_action(self):
        mine = set(self.search(self._pending_domain()).ids)
        for transfer in self:
            transfer.x_is_pending_my_action = transfer.id in mine

    def _search_is_pending_my_action(self, operator, value):
        if operator in ('in', 'not in'):
            wanted = (operator == 'in') == any(value)
        elif operator in ('=', '!='):
            wanted = (operator == '=') == bool(value)
        else:
            return NotImplemented
        domain = self._pending_domain()
        return domain if wanted else ~domain

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise UserError(_('A transfer comes from a GM decision: release the employee, or '
                              'request employees, and the GM matches them.'))
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('ksw.employee.transfer') or 'New'
            employee = self.env['hr.employee'].sudo().browse(vals.get('employee_id'))
            vals['from_department_id'] = employee.department_id.id
        transfers = super().create(vals_list)
        transfers._check_departments()
        transfers._sync_activities()
        return transfers

    def write(self, vals):
        if not self.env.su:
            if ROUTE_FIELDS & set(vals):
                raise UserError(_('Use the buttons to move a transfer on.'))
            if self.filtered(lambda t: t.state != 'hr') or not self.env.user.has_group(HR_GROUP):
                raise UserError(_('A transfer is completed by HR, only while it is with HR.'))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise UserError(_('A transfer is cancelled or returned to the GM, never deleted.'))
        return super().unlink()

    def _check_departments(self):
        for transfer in self:
            if transfer.to_department_id == transfer.from_department_id:
                raise UserError(_('%s is already in that department.', transfer.employee_id.sudo().name))

    # ------------------------------------------------------------------
    # The route
    # ------------------------------------------------------------------
    def _advance(self, state, **vals):
        self.sudo().write(dict(vals, state=state))
        self._sync_activities()

    def action_hr_approve(self):
        """HR completes what the GM decided: who he answers to, from when."""
        for transfer in self:
            if not transfer.x_can_hr:
                raise UserError(_('Only HR confirms %s at this step.', transfer.name))
            missing = [transfer._fields[f].get_description(self.env)['string'] for f in
                       ('to_manager_id', 'to_leave_manager_id', 'to_hr_responsible_id') if not transfer[f]]
            if missing:
                raise UserError(_('%(t)s: set %(f)s before confirming.', t=transfer.name, f=', '.join(missing)))
        self._advance('approved', hr_user_id=self.env.uid, hr_date=fields.Datetime.now())
        self._apply_due()
        return True

    def action_open_refuse(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Return %s to the GM', self.name),
                'res_model': 'ksw.employee.transfer.refuse.wizard', 'view_mode': 'form', 'target': 'new',
                'context': {'default_transfer_id': self.id}}

    def _refuse(self, reason):
        """HR cannot carry it out: the employee goes back to the GM's list."""
        for transfer in self:
            if not transfer.x_can_hr:
                raise UserError(_('%s cannot be returned at this step.', transfer.name))
        self._advance('refused', refuse_reason=reason, closed_by_id=self.env.uid,
                      closed_date=fields.Datetime.now())
        self._release_decision_reopened()
        gm_users = self.env.ref(GM_GROUP).sudo().all_user_ids.filtered(lambda u: u.active and not u.share)
        for transfer in self.sudo():
            transfer.message_post(body=Markup(_('Returned to the GM: %s')) % reason,
                                  partner_ids=(gm_users.partner_id | transfer.requested_by_id.partner_id).ids,
                                  subtype_xmlid='mail.mt_comment')

    def action_cancel(self):
        """The GM takes his decision back before the move happened."""
        for transfer in self:
            if not transfer.x_can_gm:
                raise UserError(_('Only the GM cancels %s, before it is applied.', transfer.name))
        self._advance('cancelled', closed_by_id=self.env.uid, closed_date=fields.Datetime.now())
        self._release_decision_reopened()
        return True

    def _release_decision_reopened(self):
        """A transfer that will not happen hands its employee back to the GM."""
        lines = self.sudo().release_line_id.filtered(lambda l: l.decision == 'transfer')
        lines.write({'decision': 'pending', 'addition_line_id': False})

    def _is_ready(self):
        """Read custody now, not the cached compute: the hand-over that
        clears it usually happens in the same transaction."""
        self.ensure_one()
        return self.with_vehicles or not self.env['ksw.fleet.vehicle'].sudo().search_count(
            [('x_custodian_id', '=', self.employee_id.id)], limit=1)

    def _apply_due(self):
        today = fields.Date.context_today(self)
        self.sudo().filtered(lambda t: t.state == 'approved' and t.effective_date <= today
                             and t._is_ready())._apply()

    def _apply(self):
        """The move itself: a new contract version from the effective date,
        in the new department. The versions before keep the old one."""
        for transfer in self.sudo():
            if transfer.state != 'approved':
                continue
            employee = transfer.employee_id
            date = transfer.effective_date
            # Not hr.employee.create_version(): it drops sudo before writing
            # the new version, so a GM (no HR rights) approving a dated
            # transfer would be refused. Same result: the version in force
            # that day, copied from that date (same contract), new department.
            base = employee._get_version(date)
            version = base if base.date_version == date else base.copy({'date_version': date})
            # Versions already planned after that date move too.
            later = employee.version_ids.filtered(lambda v: v.date_version > date)
            transfer.write({
                'from_manager_id': employee.parent_id.id,
                'from_leave_manager_id': employee.leave_manager_id.id if 'leave_manager_id' in employee._fields else False,
                'from_attendance_manager_id': employee.attendance_manager_id.id
                if 'attendance_manager_id' in employee._fields else False,
                'from_hr_responsible_id': version.hr_responsible_id.id,
            })
            version_vals = {'department_id': transfer.to_department_id.id}
            if transfer.to_hr_responsible_id:
                version_vals['hr_responsible_id'] = transfer.to_hr_responsible_id.id
            (version | later).write(version_vals)
            # Who he answers to is on the employee, not the version. An empty
            # proposal (a vehicle-with-driver move into a department with no
            # manager: no HR step) keeps what he has rather than clearing it.
            employee_vals = {}
            for src, dst in (('to_manager_id', 'parent_id'), ('to_leave_manager_id', 'leave_manager_id'),
                             ('to_attendance_manager_id', 'attendance_manager_id')):
                if transfer[src] and dst in employee._fields:
                    employee_vals[dst] = transfer[src].id
            if employee_vals:
                employee.write(employee_vals)
            transfer.write({'state': 'applied', 'version_id': version.id,
                            'applied_date': fields.Datetime.now()})
            transfer.release_line_id._sync_request_state()
            transfer.addition_line_id._sync_request_state()
            kept = [transfer._fields[f].get_description(self.env)['string'] for f in
                    ('to_manager_id', 'to_leave_manager_id', 'to_hr_responsible_id') if not transfer[f]]
            body = Markup(_(
                '<b>%(e)s</b> moved from %(f)s to <b>%(t)s</b> from %(d)s, reporting to %(m)s. '
                'Check his work schedule in the new department.')) % {
                'e': employee.name, 'f': transfer.from_department_id.name or '-',
                't': transfer.to_department_id.name, 'd': date, 'm': employee.parent_id.name or '-'}
            if kept:
                body += Markup('<br/>') + _('Not set, kept as before: %s.', ', '.join(kept))
            transfer.message_post(body=body,
                partner_ids=(transfer.requested_by_id.partner_id
                             | self.env.ref(HR_GROUP).sudo().all_user_ids.partner_id).ids,
                subtype_xmlid='mail.mt_comment')
        self._sync_activities()

    @api.model
    def _cron_apply_due(self):
        self.sudo().search([('state', '=', 'approved')])._apply_due()

    def _sync_activities(self):
        todo = self.env.ref('mail.mail_activity_data_todo')
        not_chosen = self.env.ref('base.user_root') | self.env.ref('base.user_admin')
        hr_users = self.env.ref(HR_GROUP).sudo().all_user_ids.filtered(
            lambda u: u.active and not u.share) - not_chosen
        for transfer in self.sudo().exists():
            wanted = {}
            if transfer.state == 'hr':
                for user in hr_users:
                    wanted[user.id] = _('Transfer %(e)s to %(d)s', e=transfer.employee_id.name,
                                        d=transfer.to_department_id.name)
            for activity in transfer.activity_ids.filtered(lambda a: a.activity_type_id == todo):
                if wanted.get(activity.user_id.id) == activity.summary:
                    wanted.pop(activity.user_id.id)
                else:
                    activity.unlink()
            for user_id, summary in wanted.items():
                transfer.activity_schedule(activity_type_id=todo.id, user_id=user_id, summary=summary)


class KswEmployeeTransferRefuseWizard(models.TransientModel):
    _name = 'ksw.employee.transfer.refuse.wizard'
    _description = 'Return an Employee Transfer to the GM'

    transfer_id = fields.Many2one('ksw.employee.transfer', required=True, readonly=True)
    reason = fields.Text(required=True)

    def action_confirm(self):
        self.transfer_id._refuse(self.reason)
        return {'type': 'ir.actions.act_window_close'}
