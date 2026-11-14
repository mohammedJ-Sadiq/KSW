from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.fields import Domain
from odoo.exceptions import UserError

HR_GROUP = 'KSW_employee_transfer.group_transfer_hr'
GM_GROUP = 'KSW_employee_transfer.group_transfer_gm'


class KswEmployeeTransfer(models.Model):
    """Requester → HR → GM → applied on the effective date (any day)."""
    _name = 'ksw.employee.transfer'
    _description = 'Employee Transfer'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(default='New', copy=False, readonly=True)
    employee_id = fields.Many2one('hr.employee', string='Employee', required=True, tracking=True,
                                  index=True, domain="[('department_id', 'in', allowed_department_ids)]")
    allowed_department_ids = fields.Many2many('hr.department', compute='_compute_allowed_department_ids')
    # What it was when requested: the employee's department moves on after.
    from_department_id = fields.Many2one('hr.department', string='From Department', readonly=True)
    to_department_id = fields.Many2one('hr.department', string='To Department', required=True, tracking=True)
    effective_date = fields.Date(required=True, default=fields.Date.context_today, tracking=True)
    reason = fields.Text(required=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('hr', 'HR Review'),
        ('gm', 'GM Approval'),
        ('approved', 'Approved, Waiting for Date'),
        ('applied', 'Applied'),
        ('refused', 'Refused'),
        ('cancelled', 'Cancelled'),
    ], default='draft', required=True, tracking=True, copy=False, index=True)
    requested_by_id = fields.Many2one('res.users', default=lambda self: self.env.user, readonly=True)
    refuse_reason = fields.Text(readonly=True, copy=False)
    # HR only: a contract version is not readable by a department manager.
    version_id = fields.Many2one('hr.version', string='Contract Version', readonly=True, copy=False,
                                 groups='hr.group_hr_user')
    applied_date = fields.Datetime(readonly=True, copy=False)
    # Set when the employee moves together with his vehicles (a vehicle-with-
    # driver transfer): his custody goes with him, so it does not block.
    with_vehicles = fields.Boolean(readonly=True, copy=False)
    origin = fields.Char(readonly=True, copy=False)

    custody_vehicle_ids = fields.Many2many('ksw.fleet.vehicle', compute='_compute_checks',
                                           string='Vehicles in His Custody')
    warning_text = fields.Text(compute='_compute_checks', string='Before You Approve')
    # Home ▸ Waiting for you, and the "Waiting for Me" filter.
    x_is_pending_my_action = fields.Boolean(compute='_compute_is_pending_my_action',
                                            search='_search_is_pending_my_action', string='Waiting for Me')
    x_can_hr = fields.Boolean(compute='_compute_permissions')
    x_can_gm = fields.Boolean(compute='_compute_permissions')

    @api.depends_context('uid')
    def _compute_allowed_department_ids(self):
        Department = self.env['hr.department']
        if self._is_hr_or_gm():
            allowed = Department.search([])
        else:
            allowed = Department.browse(self.env.user._ksw_transfer_department_ids())
        for transfer in self:
            transfer.allowed_department_ids = allowed

    @api.depends_context('uid')
    @api.depends('state')
    def _compute_permissions(self):
        is_hr = self.env.su or self.env.user.has_group(HR_GROUP)
        is_gm = self.env.su or self.env.user.has_group(GM_GROUP)
        for transfer in self:
            transfer.x_can_hr = transfer.state == 'hr' and is_hr
            transfer.x_can_gm = transfer.state == 'gm' and is_gm

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
        if self.custody_vehicle_ids and not self.with_vehicles:
            warnings.append(_('He answers for %s: hand them in first (Vehicle Custody: Driver Change '
                              'or a release), or move him together with them from Vehicle Allocation.',
                              ', '.join(self.custody_vehicle_ids.mapped('name'))))
        return warnings

    @api.model
    def _pending_domain(self):
        user = self.env.user
        mine = [['&', ('state', '=', 'draft'), ('requested_by_id', '=', user.id)]]
        if self.env.su or user.has_group(HR_GROUP):
            mine.append([('state', '=', 'hr')])
        if self.env.su or user.has_group(GM_GROUP):
            mine.append([('state', '=', 'gm')])
        return Domain.OR(mine)

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

    @api.model
    def _is_hr_or_gm(self):
        user = self.env.user
        return self.env.su or user.has_group(HR_GROUP) or user.has_group(GM_GROUP)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('ksw.employee.transfer') or 'New'
            if not self.env.su:
                for key in ('state', 'with_vehicles', 'version_id', 'origin', 'requested_by_id'):
                    vals.pop(key, None)
            employee = self.env['hr.employee'].sudo().browse(vals.get('employee_id'))
            vals['from_department_id'] = employee.department_id.id
        transfers = super().create(vals_list)
        transfers._check_scope()
        transfers._check_departments()
        return transfers

    def write(self, vals):
        if not self.env.su:
            if {'state', 'with_vehicles', 'version_id', 'origin', 'from_department_id'} & set(vals):
                raise UserError(_('Use the buttons to move a transfer on.'))
            if self.filtered(lambda t: t.state not in ('draft', 'hr')):
                raise UserError(_('A transfer is changed only while it is a draft or with HR.'))
        if 'employee_id' in vals:
            vals = dict(vals, from_department_id=self.env['hr.employee'].sudo().browse(
                vals['employee_id']).department_id.id)
        res = super().write(vals)
        if 'employee_id' in vals:
            self._check_scope()
        self._check_departments()
        return res

    def unlink(self):
        if not self.env.su and self.filtered(lambda t: t.state not in ('draft', 'cancelled')):
            raise UserError(_('Only a draft or cancelled transfer can be deleted.'))
        return super().unlink()

    def _check_scope(self):
        if self.env.su or self._is_hr_or_gm():
            return
        scope = set(self.env.user._ksw_transfer_department_ids())
        for transfer in self:
            if transfer.employee_id.sudo().department_id.id not in scope:
                raise UserError(_('%s is not in one of your departments.', transfer.employee_id.sudo().name))

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

    def _check_custody_cleared(self):
        for transfer in self:
            if transfer.custody_vehicle_ids and not transfer.with_vehicles:
                raise UserError(_(
                    '%(e)s still answers for %(v)s. Hand them in first (Vehicle Custody: Driver '
                    'Change, or a release), or move him together with them from Vehicle Allocation.',
                    e=transfer.employee_id.sudo().name, v=', '.join(transfer.custody_vehicle_ids.mapped('name'))))

    def action_submit(self):
        for transfer in self:
            if transfer.state != 'draft':
                raise UserError(_('%s was already submitted.', transfer.name))
        self._check_scope()
        self._advance('hr')
        return True

    def action_hr_approve(self):
        for transfer in self:
            if not transfer.x_can_hr:
                raise UserError(_('Only HR approves %s at this step.', transfer.name))
        self._check_custody_cleared()
        self._advance('gm')
        return True

    def action_gm_approve(self):
        for transfer in self:
            if not transfer.x_can_gm:
                raise UserError(_('Only the GM approves %s at this step.', transfer.name))
        self._check_custody_cleared()
        self._advance('approved')
        self.filtered(lambda t: t.effective_date <= fields.Date.context_today(t))._apply()
        return True

    def action_open_refuse(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Refuse %s', self.name),
                'res_model': 'ksw.employee.transfer.refuse.wizard', 'view_mode': 'form', 'target': 'new',
                'context': {'default_transfer_id': self.id}}

    def _refuse(self, reason):
        for transfer in self:
            if not (transfer.x_can_hr or transfer.x_can_gm):
                raise UserError(_('%s cannot be refused at this step.', transfer.name))
        self._advance('refused', refuse_reason=reason)
        for transfer in self.sudo():
            transfer.message_post(body=Markup(_('Refused: %s')) % reason,
                                  partner_ids=transfer.requested_by_id.partner_id.ids,
                                  subtype_xmlid='mail.mt_comment')

    def action_cancel(self):
        for transfer in self:
            if transfer.state not in ('draft', 'hr', 'gm', 'approved'):
                raise UserError(_('%s can no longer be cancelled.', transfer.name))
            if not self._is_hr_or_gm() and transfer.state != 'draft':
                raise UserError(_('Only HR or the GM cancels a submitted transfer.'))
        self._advance('cancelled')
        return True

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
            (version | later).write({'department_id': transfer.to_department_id.id})
            transfer.write({'state': 'applied', 'version_id': version.id,
                            'applied_date': fields.Datetime.now()})
            transfer.message_post(body=Markup(_(
                '<b>%(e)s</b> moved from %(f)s to <b>%(t)s</b> from %(d)s. Check his manager '
                'and work schedule in the new department.')) % {
                'e': employee.name, 'f': transfer.from_department_id.name or '-',
                't': transfer.to_department_id.name, 'd': date},
                partner_ids=(transfer.requested_by_id.partner_id
                             | self.env.ref(HR_GROUP).sudo().all_user_ids.partner_id).ids,
                subtype_xmlid='mail.mt_comment')
        self._sync_activities()

    @api.model
    def _cron_apply_due(self):
        due = self.sudo().search([('state', '=', 'approved'),
                                  ('effective_date', '<=', fields.Date.context_today(self))])
        due._apply()

    def _sync_activities(self):
        todo = self.env.ref('mail.mail_activity_data_todo')
        hr_users = self.env.ref(HR_GROUP).sudo().all_user_ids.filtered(lambda u: u.active and not u.share)
        gm_users = self.env.ref(GM_GROUP).sudo().all_user_ids.filtered(lambda u: u.active and not u.share)
        for transfer in self.sudo().exists():
            wanted = {}
            users = hr_users if transfer.state == 'hr' else gm_users if transfer.state == 'gm' else ()
            for user in users:
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
    _description = 'Refuse an Employee Transfer'

    transfer_id = fields.Many2one('ksw.employee.transfer', required=True, readonly=True)
    reason = fields.Text(required=True)

    def action_confirm(self):
        self.transfer_id._refuse(self.reason)
        return {'type': 'ir.actions.act_window_close'}
