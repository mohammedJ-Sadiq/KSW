from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.fields import Domain
from odoo.exceptions import UserError

HR_GROUP = 'KSW_employee_transfer.group_transfer_hr'
GM_GROUP = 'KSW_employee_transfer.group_transfer_gm'


class KswEmployeeRequest(models.Model):
    """A department's intent, as in Vehicle Allocation: it releases employees
    it no longer needs, or asks for employees. Only the GM decides; HR
    carries out what he decided (transfer, end of service, hiring)."""
    _name = 'ksw.employee.request'
    _description = 'Employee Release / Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(default='New', copy=False, readonly=True)
    request_type = fields.Selection([
        ('release', 'Release Employees'),
        ('addition', 'Request Employees'),
    ], required=True, default='release', tracking=True)
    department_id = fields.Many2one('hr.department', string='Department', required=True, tracking=True,
                                    index=True, domain="[('id', 'in', allowed_department_ids)]",
                                    default=lambda self: self._default_department_id())
    allowed_department_ids = fields.Many2many('hr.department', compute='_compute_allowed_department_ids')
    # Attached to one department only: it is preselected and cannot be changed.
    x_department_locked = fields.Boolean(compute='_compute_allowed_department_ids')
    requested_by_id = fields.Many2one('res.users', string='Requested By', default=lambda self: self.env.user,
                                      readonly=True)
    reason = fields.Text(required=True)
    submitted_date = fields.Datetime(readonly=True, copy=False)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),
        ('done', 'Done'),
        ('cancelled', 'Cancelled'),
    ], default='draft', required=True, tracking=True, copy=False, index=True)
    release_line_ids = fields.One2many('ksw.employee.release.line', 'request_id', string='Employees to Release')
    addition_line_ids = fields.One2many('ksw.employee.addition.line', 'request_id', string='Employees Needed')
    line_summary = fields.Char(compute='_compute_line_summary', string='Summary')
    # Who the request waits for: stored, so Home and the filters can search it.
    x_waiting_gm = fields.Boolean(compute='_compute_waiting', store=True, string='Waiting for the GM')
    x_waiting_hr = fields.Boolean(compute='_compute_waiting', store=True, string='Waiting for HR')
    x_is_pending_my_action = fields.Boolean(compute='_compute_is_pending_my_action',
                                            search='_search_is_pending_my_action', string='Waiting for Me')
    x_is_gm = fields.Boolean(compute='_compute_roles')
    x_is_hr = fields.Boolean(compute='_compute_roles')

    @api.model
    def _home_departments(self):
        """What the current user raises requests for: every department for
        HR and the GM, else those he is attached to."""
        Department = self.env['hr.department']
        if self._is_hr_or_gm():
            return Department.search([])
        return Department.browse(self.env.user._ksw_transfer_home_department_ids())

    @api.model
    def _default_department_id(self):
        if self._is_hr_or_gm():
            return False
        homes = self._home_departments()
        return homes.id if len(homes) == 1 else False

    @api.depends_context('uid')
    def _compute_allowed_department_ids(self):
        allowed = self._home_departments()
        locked = not self._is_hr_or_gm() and len(allowed) == 1
        for request in self:
            request.allowed_department_ids = allowed
            request.x_department_locked = locked

    @api.depends_context('uid')
    def _compute_roles(self):
        is_gm = self.env.su or self.env.user.has_group(GM_GROUP)
        is_hr = self.env.su or self.env.user.has_group(HR_GROUP)
        for request in self:
            request.x_is_gm = is_gm
            request.x_is_hr = is_hr

    @api.depends('release_line_ids.employee_id', 'addition_line_ids.quantity', 'addition_line_ids.job_id')
    def _compute_line_summary(self):
        for request in self:
            if request.request_type == 'release':
                request.line_summary = ', '.join(request.release_line_ids.employee_id.sudo().mapped('name'))
            else:
                request.line_summary = ', '.join('%s × %s' % (l.quantity, l.job_id.name or '-')
                                                 for l in request.addition_line_ids)

    @api.depends('state', 'release_line_ids.status', 'addition_line_ids.status')
    def _compute_waiting(self):
        for request in self:
            statuses = set(request.release_line_ids.mapped('status')) | set(request.addition_line_ids.mapped('status'))
            open_request = request.state == 'submitted'
            request.x_waiting_gm = open_request and 'gm' in statuses
            request.x_waiting_hr = open_request and bool({'terminating', 'hiring'} & statuses)

    @api.model
    def _is_hr_or_gm(self):
        user = self.env.user
        return self.env.su or user.has_group(HR_GROUP) or user.has_group(GM_GROUP)

    @api.model
    def _pending_domain(self):
        user = self.env.user
        mine = [Domain('state', '=', 'draft') & Domain('requested_by_id', '=', user.id)]
        if self.env.su or user.has_group(GM_GROUP):
            mine.append(Domain('x_waiting_gm', '=', True))
        if self.env.su or user.has_group(HR_GROUP):
            mine.append(Domain('x_waiting_hr', '=', True))
        return Domain.OR(mine)

    @api.depends_context('uid')
    def _compute_is_pending_my_action(self):
        mine = set(self.search(self._pending_domain()).ids)
        for request in self:
            request.x_is_pending_my_action = request.id in mine

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
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('ksw.employee.request') or 'New'
            if not self.env.su:
                for key in ('state', 'submitted_date', 'requested_by_id'):
                    vals.pop(key, None)
        requests = super().create(vals_list)
        requests._check_scope()
        return requests

    def write(self, vals):
        if not self.env.su:
            if {'state', 'submitted_date', 'requested_by_id'} & set(vals):
                raise UserError(_('Use the buttons to move a request on.'))
            if self.filtered(lambda r: r.state != 'draft'):
                raise UserError(_('A submitted request is not changed: the GM decides it as sent.'))
        res = super().write(vals)
        if 'department_id' in vals:
            self._check_scope()
        return res

    def unlink(self):
        if not self.env.su and self.filtered(lambda r: r.state not in ('draft', 'cancelled')):
            raise UserError(_('Only a draft or cancelled request can be deleted.'))
        return super().unlink()

    def _check_scope(self):
        if self.env.su or self._is_hr_or_gm():
            return
        scope = set(self.env.user._ksw_transfer_department_ids())
        for request in self:
            if request.department_id.id not in scope:
                raise UserError(_('You do not answer for the employees of %s.', request.department_id.name))

    # ------------------------------------------------------------------
    # Requester
    # ------------------------------------------------------------------
    def action_submit(self):
        self._check_scope()
        for request in self:
            if request.state != 'draft':
                raise UserError(_('%s was already submitted.', request.name))
            if request.request_type == 'release':
                if not request.release_line_ids:
                    raise UserError(_('Add the employees you release.'))
                if request.addition_line_ids:
                    raise UserError(_('A release lists employees to release only.'))
                request.release_line_ids._check_releasable()
            else:
                if not request.addition_line_ids:
                    raise UserError(_('Add the job and number of employees you need.'))
                if request.release_line_ids:
                    raise UserError(_('A request for employees lists the employees needed only.'))
        self.sudo().write({'state': 'submitted', 'submitted_date': fields.Datetime.now()})
        for request in self.sudo():
            request.message_post(body=Markup(
                _('<b>%(t)s</b> from <b>%(d)s</b> waits for the GM\'s decision: %(s)s<br/><i>%(r)s</i>'))
                % {'t': dict(request._fields['request_type']._description_selection(self.env))[request.request_type],
                   'd': request.department_id.name, 's': request.line_summary, 'r': request.reason}
                + request._submit_note_extra(),
                subtype_xmlid='mail.mt_note')
        self._sync_activities()
        return True

    def _submit_note_extra(self):
        """More for the GM's note at submit (hook: the vehicles bridge)."""
        return Markup('')

    def action_cancel(self):
        for request in self:
            if request.state not in ('draft', 'submitted'):
                raise UserError(_('%s can no longer be cancelled.', request.name))
            decided = request.release_line_ids.filtered(lambda l: l.decision not in ('pending', 'hold')) \
                or request.addition_line_ids.filtered(lambda l: l.given_count or l.external_quantity)
            if decided:
                raise UserError(_('The GM already decided part of %s: it can no longer be cancelled.',
                                  request.name))
            if request.state == 'submitted' and not (self._is_hr_or_gm() or request.requested_by_id == self.env.user):
                raise UserError(_('Only the requester, HR or the GM cancels a submitted request.'))
        self.sudo().write({'state': 'cancelled'})
        self._sync_activities()
        return True

    # ------------------------------------------------------------------
    # GM and HR entry points (one home: the request)
    # ------------------------------------------------------------------
    def action_open_decide(self):
        self.ensure_one()
        if not self.x_is_gm:
            raise UserError(_('Only the GM decides employee requests.'))
        if self.request_type == 'release':
            lines = self.release_line_ids.filtered(lambda l: l.status in ('gm', 'hold'))
            return lines.action_open_decide()
        line = self.addition_line_ids.filtered(lambda l: l.status in ('gm', 'hold'))[:1]
        if not line:
            raise UserError(_('Nothing left to decide on %s.', self.name))
        return line.action_open_decide()

    def _check_lines_editable(self):
        if self.env.su:
            return
        for request in self:
            if request.state != 'draft':
                raise UserError(_('%s is submitted: its lines are the GM\'s to decide.', request.name))

    def _sync_state(self):
        for request in self.sudo().filtered(lambda r: r.state == 'submitted'):
            closed = request.release_line_ids.mapped('is_closed') + request.addition_line_ids.mapped('is_closed')
            if closed and all(closed):
                request.state = 'done'
        self._sync_activities()

    def _sync_activities(self):
        """Desired-vs-actual: one to-do per person the request waits for."""
        todo = self.env.ref('mail.mail_activity_data_todo')
        not_chosen = self.env.ref('base.user_root') | self.env.ref('base.user_admin')

        def holders(xmlid):
            return self.env.ref(xmlid).sudo().all_user_ids.filtered(lambda u: u.active and not u.share) - not_chosen

        gm_users, hr_users = holders(GM_GROUP), holders(HR_GROUP)
        for request in self.sudo().exists():
            wanted = {}
            if request.x_waiting_gm:
                for user in gm_users:
                    wanted[user.id] = _('Decide %s', request.name)
            if request.x_waiting_hr:
                for user in hr_users:
                    wanted.setdefault(user.id, _('Carry out %s', request.name))
            for activity in request.activity_ids.filtered(lambda a: a.activity_type_id == todo):
                if wanted.get(activity.user_id.id) == activity.summary:
                    wanted.pop(activity.user_id.id)
                else:
                    activity.unlink()
            for user_id, summary in wanted.items():
                request.activity_schedule(activity_type_id=todo.id, user_id=user_id, summary=summary)


class KswEmployeeReleaseLine(models.Model):
    """One employee a department gives up. The GM gives him to another
    department's request, keeps him on hold (he stays and works, offered to
    the next request), ends his service, or refuses the release."""
    _name = 'ksw.employee.release.line'
    _description = 'Released Employee'
    _order = 'request_id desc, id'
    _rec_name = 'employee_id'

    request_id = fields.Many2one('ksw.employee.request', required=True, index=True, ondelete='cascade')
    request_state = fields.Selection(related='request_id.state', string='Request Status')
    department_id = fields.Many2one(related='request_id.department_id', store=True, string='Department')
    employee_id = fields.Many2one('hr.employee', string='Employee', required=True, index=True)
    job_id = fields.Many2one('hr.job', string='Job Position', compute='_compute_job_id', store=True)
    note = fields.Char(string='Why')
    decision = fields.Selection([
        ('pending', 'Not Decided'),
        ('transfer', 'Transfer'),
        ('hold', 'On Hold'),
        ('terminate', 'End of Service'),
        ('keep', 'Refused (Stays)'),
    ], default='pending', required=True, copy=False)
    addition_line_id = fields.Many2one('ksw.employee.addition.line', string='Given To', copy=False, index=True)
    decision_note = fields.Text(string='GM Note', copy=False)
    decided_by_id = fields.Many2one('res.users', string='Decided By', copy=False)
    decided_date = fields.Datetime(string='Decided On', copy=False)
    transfer_ids = fields.One2many('ksw.employee.transfer', 'release_line_id', string='Transfers')
    transfer_id = fields.Many2one('ksw.employee.transfer', compute='_compute_transfer_id', string='Transfer')
    # HR's end-of-service step; extended by the EOS bridge.
    terminated = fields.Boolean(string='Service Ended', copy=False)
    status = fields.Selection([
        ('draft', 'Draft'),
        ('gm', 'Waiting for the GM'),
        ('hold', 'On Hold'),
        ('transferring', 'Transfer with HR'),
        ('transferred', 'Transferred'),
        ('terminating', 'End of Service with HR'),
        ('terminated', 'Service Ended'),
        ('kept', 'Stays'),
        ('cancelled', 'Cancelled'),
    ], compute='_compute_status', store=True, index=True)
    is_closed = fields.Boolean(compute='_compute_status', store=True)

    @api.depends('employee_id')
    def _compute_job_id(self):
        for line in self:
            line.job_id = line.employee_id.sudo().job_id

    @api.depends('transfer_ids.state')
    def _compute_transfer_id(self):
        for line in self:
            line.transfer_id = line.transfer_ids.filtered(
                lambda t: t.state not in ('refused', 'cancelled')).sorted('id')[-1:]

    @api.depends('request_id.state', 'decision', 'terminated', 'transfer_ids.state')
    def _compute_status(self):
        for line in self:
            state = line.request_id.state
            live = line.transfer_ids.filtered(lambda t: t.state not in ('refused', 'cancelled'))
            if state == 'draft':
                status = 'draft'
            elif state == 'cancelled':
                status = 'cancelled'
            elif line.decision == 'pending':
                status = 'gm'
            elif line.decision == 'hold':
                status = 'hold'
            elif line.decision == 'keep':
                status = 'kept'
            elif line.decision == 'transfer':
                status = 'transferred' if live.filtered(lambda t: t.state == 'applied') else 'transferring'
            else:
                status = 'terminated' if line.terminated else 'terminating'
            line.status = status
            line.is_closed = status in ('transferred', 'terminated', 'kept', 'cancelled')

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        lines.request_id._check_lines_editable()
        return lines

    def write(self, vals):
        if not self.env.su and set(vals) - {'employee_id', 'note'}:
            raise UserError(_('The GM decides released employees through his decision.'))
        if not self.env.su:
            self.request_id._check_lines_editable()
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            self.request_id._check_lines_editable()
        return super().unlink()

    def _check_releasable(self):
        """At submit: his own department's people, released once at a time."""
        for line in self:
            employee = line.employee_id.sudo()
            department = line.request_id.department_id
            if employee.department_id not in self.env['hr.department'].sudo().search(
                    [('id', 'child_of', department.id)]):
                raise UserError(_('%(e)s is not in %(d)s.', e=employee.name, d=department.name))
            other = self.sudo().search([('employee_id', '=', employee.id), ('id', '!=', line.id),
                                        ('is_closed', '=', False), ('request_id.state', '=', 'submitted')], limit=1)
            if other:
                raise UserError(_('%(e)s is already released on %(r)s.', e=employee.name, r=other.request_id.name))
        if len(self.employee_id) != len(self):
            raise UserError(_('An employee is listed twice.'))

    def action_open_decide(self):
        if not (self.env.su or self.env.user.has_group(GM_GROUP)):
            raise UserError(_('Only the GM decides released employees.'))
        lines = self.filtered(lambda l: l.status in ('gm', 'hold'))
        if not lines:
            raise UserError(_('Nothing left to decide here.'))
        return {
            'type': 'ir.actions.act_window', 'name': _('Decide Released Employees'),
            'res_model': 'ksw.employee.release.decision.wizard', 'view_mode': 'form', 'target': 'new',
            'context': {'default_release_line_ids': [(6, 0, lines.ids)]},
        }

    def _ksw_decide(self, decision, addition_line=False, effective_date=False, note=False):
        """The GM's decision, from either side (released employee or request).
        Transfer and end of service take him away: that is when his vehicles
        are dealt with (`_ksw_on_leaving`, Vehicle Custody bridge)."""
        if not (self.env.su or self.env.user.has_group(GM_GROUP)):
            raise UserError(_('Only the GM decides released employees.'))
        for line in self:
            if line.status not in ('gm', 'hold'):
                raise UserError(_('%s is already decided.', line.employee_id.sudo().name))
        if decision == 'transfer':
            addition_line._ksw_check_fills(self, note)
        stamp = {'decision': decision, 'decision_note': note, 'decided_by_id': self.env.uid,
                 'decided_date': fields.Datetime.now(),
                 'addition_line_id': addition_line.id if decision == 'transfer' else False}
        self.sudo().write(stamp)
        if decision == 'transfer':
            Transfer = self.env['ksw.employee.transfer'].sudo()
            for line in self.sudo():
                Transfer.create({
                    'employee_id': line.employee_id.id,
                    'to_department_id': addition_line.department_id.id,
                    'effective_date': effective_date or fields.Date.context_today(self),
                    'reason': line.note or line.request_id.reason,
                    'release_line_id': line.id, 'addition_line_id': addition_line.id,
                    'requested_by_id': addition_line.request_id.requested_by_id.id,
                    'origin': '%s → %s' % (line.request_id.name, addition_line.request_id.name),
                    'gm_user_id': self.env.uid, 'gm_date': fields.Datetime.now(),
                })
        if decision in ('transfer', 'terminate'):
            self._ksw_on_leaving()
        if decision == 'terminate':
            self._ksw_on_terminate(effective_date or fields.Date.context_today(self))
        self._ksw_post_decision(decision, addition_line, note)
        (self.request_id | addition_line.request_id if addition_line else self.request_id)._sync_state()
        return True

    def _ksw_on_leaving(self):
        """Hook: the employee leaves his department (vehicles, …)."""
        return True

    def _ksw_on_terminate(self, date):
        """Hook: an end of service decided for `date`. The EOS bridge opens
        the End of Service request in Time Off; without it HR records it here."""
        return True

    def _ksw_post_decision(self, decision, addition_line, note):
        labels = dict(self._fields['decision']._description_selection(self.env))
        for request in self.sudo().request_id:
            lines = self.filtered(lambda l: l.request_id == request)
            target = _(' to %(d)s (%(r)s)', d=addition_line.department_id.name,
                       r=addition_line.request_id.name) if addition_line else ''
            body = Markup(_('GM decision: <b>%(dec)s</b>%(t)s for %(e)s.')) % {
                'dec': labels[decision], 't': target, 'e': ', '.join(lines.employee_id.mapped('name'))}
            if note:
                body += Markup('<br/><i>%s</i>') % note
            partners = request.requested_by_id.partner_id
            if decision == 'transfer':
                partners |= self.env.ref(HR_GROUP).sudo().all_user_ids.filtered(
                    lambda u: u.active and not u.share).partner_id
            request.message_post(body=body, partner_ids=partners.ids, subtype_xmlid='mail.mt_comment')
            if addition_line:
                addition_line.request_id.message_post(
                    body=body, partner_ids=addition_line.request_id.requested_by_id.partner_id.ids,
                    subtype_xmlid='mail.mt_comment')

    def action_hr_terminate(self):
        """HR's step on an end of service. The EOS bridge replaces it with the
        End of Service request itself; without it HR records that it is done."""
        if not (self.env.su or self.env.user.has_group(HR_GROUP)):
            raise UserError(_('Only HR carries out an end of service.'))
        for line in self:
            if line.status != 'terminating':
                raise UserError(_('%s is not waiting for an end of service.', line.employee_id.sudo().name))
        self.sudo().write({'terminated': True})
        self._sync_request_state()
        return True

    def _sync_request_state(self):
        self.request_id._sync_state()


class KswEmployeeAdditionLine(models.Model):
    """A department needs N employees of a job. The GM fills it from released
    employees, approves hiring from outside (HR names the new hires), keeps
    it on hold, or refuses it."""
    _name = 'ksw.employee.addition.line'
    _description = 'Employees Needed'
    _order = 'request_id desc, id'

    request_id = fields.Many2one('ksw.employee.request', required=True, index=True, ondelete='cascade')
    request_state = fields.Selection(related='request_id.state', string='Request Status')
    department_id = fields.Many2one(related='request_id.department_id', store=True, string='Department')
    job_id = fields.Many2one('hr.job', string='Job Position', required=True)
    quantity = fields.Integer(required=True, default=1)
    note = fields.Char(string='Why / Requirements')
    given_line_ids = fields.One2many('ksw.employee.release.line', 'addition_line_id', string='Given Employees')
    given_count = fields.Integer(compute='_compute_quantities', store=True, string='Transferred In')
    external_quantity = fields.Integer(string='Hire from Outside', readonly=True, copy=False)
    refused_quantity = fields.Integer(string='Refused', readonly=True, copy=False)
    on_hold = fields.Boolean(readonly=True, copy=False)
    decision_note = fields.Text(string='GM Note', readonly=True, copy=False)
    hired_employee_ids = fields.Many2many('hr.employee', 'ksw_employee_addition_hired_rel', 'line_id',
                                          'employee_id', string='Hired', readonly=True, copy=False)
    open_quantity = fields.Integer(compute='_compute_quantities', store=True, string='Open')
    hire_open = fields.Integer(compute='_compute_quantities', store=True, string='Hires to Name')
    status = fields.Selection([
        ('draft', 'Draft'),
        ('gm', 'Waiting for the GM'),
        ('hold', 'On Hold'),
        ('hiring', 'Hiring with HR'),
        ('done', 'Done'),
        ('cancelled', 'Cancelled'),
    ], compute='_compute_quantities', store=True, index=True)
    is_closed = fields.Boolean(compute='_compute_quantities', store=True)

    @api.depends('quantity', 'given_line_ids.decision', 'external_quantity', 'refused_quantity',
                 'hired_employee_ids', 'on_hold', 'request_id.state')
    def _compute_quantities(self):
        for line in self:
            given = len(line.given_line_ids.filtered(lambda l: l.decision == 'transfer'))
            line.given_count = given
            line.open_quantity = max(line.quantity - given - line.external_quantity - line.refused_quantity, 0)
            line.hire_open = max(line.external_quantity - len(line.hired_employee_ids), 0)
            state = line.request_id.state
            if state == 'draft':
                status = 'draft'
            elif state == 'cancelled':
                status = 'cancelled'
            elif line.open_quantity:
                status = 'hold' if line.on_hold else 'gm'
            elif line.hire_open:
                status = 'hiring'
            else:
                status = 'done'
            line.status = status
            line.is_closed = status in ('done', 'cancelled')

    @api.constrains('quantity')
    def _check_quantity(self):
        for line in self:
            if line.quantity < 1:
                raise UserError(_('The number of employees needed must be at least 1.'))

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        lines.request_id._check_lines_editable()
        return lines

    def write(self, vals):
        if not self.env.su:
            if set(vals) - {'job_id', 'quantity', 'note'}:
                raise UserError(_('The GM decides requests through his decision.'))
            self.request_id._check_lines_editable()
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            self.request_id._check_lines_editable()
        return super().unlink()

    def _compute_display_name(self):
        for line in self:
            line.display_name = '%s: %s × %s (%s)' % (line.request_id.name, line.open_quantity,
                                                      line.job_id.name or '-', line.department_id.name or '-')

    def _ksw_check_fills(self, release_lines, note):
        """Released employees go only to a department that asked: an open
        request, room left, another department. Another job is the GM's
        call (the wizard warns; a note is welcome, not required)."""
        if len(self) != 1 or self.sudo().request_id.state != 'submitted':
            raise UserError(_('Choose the open request the employees go to.'))
        line = self.sudo()
        if len(release_lines) > line.open_quantity:
            raise UserError(_('%(r)s needs %(n)s more, not %(m)s.', r=line.display_name,
                              n=line.open_quantity, m=len(release_lines)))
        for release in release_lines.sudo():
            if release.department_id == line.department_id:
                raise UserError(_('%s would go back to his own department.', release.employee_id.name))

    def action_open_decide(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group(GM_GROUP)):
            raise UserError(_('Only the GM decides employee requests.'))
        if self.status not in ('gm', 'hold'):
            raise UserError(_('Nothing left to decide on this line.'))
        return {
            'type': 'ir.actions.act_window', 'name': _('Decide %s', self.display_name),
            'res_model': 'ksw.employee.addition.decision.wizard', 'view_mode': 'form', 'target': 'new',
            'context': {'default_addition_line_id': self.id},
        }

    def _ksw_decide_other(self, decision, quantity, note):
        """External hiring, refusal or hold: the decisions with no employee."""
        if not (self.env.su or self.env.user.has_group(GM_GROUP)):
            raise UserError(_('Only the GM decides employee requests.'))
        self.ensure_one()
        line = self.sudo()
        if decision == 'hold':
            line.write({'on_hold': True, 'decision_note': note})
        else:
            if quantity < 1 or quantity > line.open_quantity:
                raise UserError(_('Choose between 1 and %s.', line.open_quantity))
            field = 'external_quantity' if decision == 'external' else 'refused_quantity'
            line.write({field: line[field] + quantity, 'on_hold': False, 'decision_note': note})
        labels = {'external': _('Hire from outside'), 'refuse': _('Refused'), 'hold': _('On hold')}
        body = Markup(_('GM decision on %(l)s: <b>%(d)s</b>%(q)s.')) % {
            'l': line.job_id.name, 'd': labels[decision],
            'q': ' × %s' % quantity if decision != 'hold' else ''}
        if note:
            body += Markup('<br/><i>%s</i>') % note
        partners = line.request_id.requested_by_id.partner_id
        if decision == 'external':
            partners |= self.env.ref(HR_GROUP).sudo().all_user_ids.filtered(
                lambda u: u.active and not u.share).partner_id
        line.request_id.message_post(body=body, partner_ids=partners.ids, subtype_xmlid='mail.mt_comment')
        line.request_id._sync_state()
        return True

    def action_open_hire(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group(HR_GROUP)):
            raise UserError(_('Only HR names the new hires.'))
        return {
            'type': 'ir.actions.act_window', 'name': _('New Hires for %s', self.display_name),
            'res_model': 'ksw.employee.hire.wizard', 'view_mode': 'form', 'target': 'new',
            'context': {'default_addition_line_id': self.id},
        }

    def _ksw_record_hires(self, employees):
        if not (self.env.su or self.env.user.has_group(HR_GROUP)):
            raise UserError(_('Only HR names the new hires.'))
        self.ensure_one()
        line = self.sudo()
        if len(employees) > line.hire_open:
            raise UserError(_('The GM approved %s more hires, not %s.', line.hire_open, len(employees)))
        scope = self.env['hr.department'].sudo().search([('id', 'child_of', line.department_id.id)])
        for employee in employees.sudo():
            if employee.department_id not in scope:
                raise UserError(_('%(e)s is not in %(d)s: hire him into the department that asked.',
                                  e=employee.name, d=line.department_id.name))
        line.write({'hired_employee_ids': [(4, e.id) for e in employees]})
        line.request_id.message_post(body=Markup(_('Hired for %(l)s: %(e)s.')) % {
            'l': line.job_id.name, 'e': ', '.join(employees.sudo().mapped('name'))},
            partner_ids=line.request_id.requested_by_id.partner_id.ids, subtype_xmlid='mail.mt_comment')
        line.request_id._sync_state()
        return True

    def _sync_request_state(self):
        self.request_id._sync_state()

