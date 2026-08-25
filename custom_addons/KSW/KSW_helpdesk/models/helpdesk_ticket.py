from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

STATUS_FIELDS = {'stage_id', 'kanban_state', 'user_id'}
SEQUENCE_CODE_BY_TYPE = {
    'incident': 'helpdesk.ticket.incident',
    'request': 'helpdesk.ticket.request',
}


class HelpdeskTicket(models.Model):
    _name = 'helpdesk.ticket'
    _description = 'Helpdesk Ticket'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'priority desc, create_date desc, id desc'

    # ------------------------------------------------------------------
    # Identification
    # ------------------------------------------------------------------
    ticket_ref = fields.Char(
        string='Reference', required=True, copy=False, readonly=True, default='New',
    )
    ticket_type = fields.Selection([
        ('incident', 'Incident'),
        ('request', 'Service Request'),
    ], required=True, default='incident', tracking=True,
        help="Incident: something is broken or not working as expected.\n"
             "Service Request: a planned ask - new access, new software, "
             "information, etc.\n"
             "Fixed once the ticket is created (it decides the ticket "
             "number prefix: INC/... or REQ/...).",
    )
    name = fields.Char(string='Short Description', required=True, tracking=True)
    description = fields.Html(string='Detailed Description', required=True)
    employee_id = fields.Many2one(
        'hr.employee', string='Reported By', required=True, tracking=True,
        default=lambda self: self.env.user.employee_id,
        domain=lambda self: self._domain_employee_id(),
        help="Who the ticket is for. You can only pick yourself, unless "
             "you are a manager - then you may also pick a direct report.",
    )
    category_id = fields.Many2one(
        'helpdesk.ticket.category', string='Category', tracking=True, required=True,
        domain="[('ticket_type', '=', ticket_type)]",
    )
    asset_id = fields.Many2one(
        'it.asset', string='Related Asset',
        domain="[('employee_id', '=', employee_id)]",
        help="The IT asset this ticket is about, if any.",
    )

    # ------------------------------------------------------------------
    # Caller card (read-only mirrors of the requester's contact details -
    # sudo compute rather than related=, so agents/managers who aren't HR
    # users can still see who they're helping; see KSW_deduction's
    # "Employee detail mirrors" for the same pattern in this codebase).
    # ------------------------------------------------------------------
    caller_email = fields.Char(compute='_compute_caller_info', string='Email')
    caller_work_phone = fields.Char(compute='_compute_caller_info', string='Work Phone')
    caller_mobile_phone = fields.Char(compute='_compute_caller_info', string='Mobile')
    caller_job_title = fields.Char(compute='_compute_caller_info', string='Job Position')
    caller_department_id = fields.Many2one(
        'hr.department', compute='_compute_caller_info', string='Department',
    )

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    priority = fields.Selection([
        ('0', 'Low'),
        ('1', 'Medium'),
        ('2', 'High'),
        ('3', 'Urgent'),
    ], default='1', tracking=True)
    stage_id = fields.Many2one(
        'helpdesk.ticket.stage', string='Stage', tracking=True,
        group_expand='_read_group_stage_ids',
        default=lambda self: self._default_stage_id(),
        ondelete='restrict',
    )
    kanban_state = fields.Selection([
        ('normal', 'In Progress'),
        ('blocked', 'Blocked'),
        ('done', 'Ready'),
    ], default='normal', string='Kanban State', tracking=True)
    is_closed = fields.Boolean(related='stage_id.is_closed', store=True, readonly=True)
    user_id = fields.Many2one(
        'res.users', string='Assigned To', tracking=True,
        domain=lambda self: [
            ('all_group_ids', 'in', self.env.ref('KSW_helpdesk.group_helpdesk_agent').id),
        ],
        help="Only IT Helpdesk agents/managers can be assigned a ticket.",
    )
    deadline = fields.Date()
    is_overdue = fields.Boolean(compute='_compute_is_overdue', search='_search_is_overdue')
    close_date = fields.Datetime(readonly=True, copy=False)
    closed_by = fields.Many2one('res.users', readonly=True, copy=False)
    satisfaction = fields.Selection([
        ('great', 'Great'),
        ('okay', 'Okay'),
        ('bad', 'Not Good'),
    ], copy=False, tracking=True)
    active = fields.Boolean(default=True)
    color = fields.Integer(related='category_id.color', store=True, readonly=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    resolution_hours = fields.Float(
        string='Resolution Time (h)', compute='_compute_resolution_hours', store=True,
        help="Hours between creation and closing. Used for reporting on solved tickets.",
    )

    # ------------------------------------------------------------------
    # Compute
    # ------------------------------------------------------------------
    def _default_stage_id(self):
        return self.env['helpdesk.ticket.stage'].search([], order='sequence', limit=1)

    def _domain_employee_id(self):
        if self.env.su or self.env.user.has_group('KSW_helpdesk.group_helpdesk_agent'):
            return []
        return [
            '|',
            ('user_id', '=', self.env.user.id),
            ('parent_id.user_id', '=', self.env.user.id),
        ]

    @api.model
    def _read_group_stage_ids(self, stages, domain):
        return stages.search([], order='sequence')

    @api.depends('employee_id')
    def _compute_caller_info(self):
        for ticket in self:
            employee = ticket.employee_id.sudo()
            ticket.caller_email = employee.work_email
            ticket.caller_work_phone = employee.work_phone
            ticket.caller_mobile_phone = employee.mobile_phone
            ticket.caller_job_title = employee.job_title
            ticket.caller_department_id = employee.department_id

    @api.depends('deadline', 'is_closed')
    def _compute_is_overdue(self):
        today = fields.Date.context_today(self)
        for ticket in self:
            ticket.is_overdue = bool(
                ticket.deadline and ticket.deadline < today and not ticket.is_closed
            )

    @api.depends('create_date', 'close_date')
    def _compute_resolution_hours(self):
        for ticket in self:
            if ticket.create_date and ticket.close_date:
                delta = ticket.close_date - ticket.create_date
                ticket.resolution_hours = delta.total_seconds() / 3600.0
            else:
                ticket.resolution_hours = 0.0

    def _search_is_overdue(self, operator, value):
        if operator in ('in', 'not in'):
            wanted_overdue = (operator == 'in') == any(value)
        elif operator in ('=', '!='):
            wanted_overdue = (operator == '=') == bool(value)
        else:
            return NotImplemented
        today = fields.Date.context_today(self)
        overdue_domain = [('deadline', '<', today), ('is_closed', '=', False)]
        if wanted_overdue:
            return overdue_domain
        return ['!'] + overdue_domain

    @api.onchange('ticket_type')
    def _onchange_ticket_type(self):
        if self.category_id and self.category_id.ticket_type != self.ticket_type:
            self.category_id = False

    @api.constrains('ticket_type', 'category_id')
    def _check_category_matches_type(self):
        for ticket in self:
            if ticket.category_id and ticket.category_id.ticket_type != ticket.ticket_type:
                raise ValidationError(_(
                    'The category "%(category)s" is not valid for a %(type)s ticket.',
                    category=ticket.category_id.name,
                    type=dict(ticket._fields['ticket_type'].selection)[ticket.ticket_type],
                ))

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('ticket_ref', 'New') == 'New':
                seq_code = SEQUENCE_CODE_BY_TYPE.get(
                    vals.get('ticket_type', 'incident'), SEQUENCE_CODE_BY_TYPE['incident'],
                )
                vals['ticket_ref'] = self.env['ir.sequence'].next_by_code(seq_code) or 'New'
            self._check_assignee(vals.get('user_id'))
            self._check_employee_scope(vals.get('employee_id'))
        tickets = super().create(vals_list)
        tickets._notify_assignment()
        return tickets

    def write(self, vals):
        if (
            not self.env.su
            and STATUS_FIELDS.intersection(vals)
            and not self.env.user.has_group('KSW_helpdesk.group_helpdesk_agent')
        ):
            raise UserError(_(
                'Only IT Helpdesk agents can change the stage, kanban state '
                'or assignee of a ticket.'
            ))
        if 'user_id' in vals:
            self._check_assignee(vals['user_id'])
        if 'employee_id' in vals:
            self._check_employee_scope(vals['employee_id'])
        result = super().write(vals)
        if 'user_id' in vals:
            self._notify_assignment()
        return result

    def _check_employee_scope(self, employee_id):
        if not employee_id or self.env.su or self.env.user.has_group('KSW_helpdesk.group_helpdesk_agent'):
            return
        employee = self.env['hr.employee'].browse(employee_id)
        if employee.user_id.id != self.env.user.id and employee.parent_id.user_id.id != self.env.user.id:
            raise UserError(_(
                'You can only create or edit tickets for yourself or your '
                'direct reports.'
            ))

    def _check_assignee(self, user_id):
        if not user_id:
            return
        agent_group = self.env.ref('KSW_helpdesk.group_helpdesk_agent')
        if agent_group not in self.env['res.users'].browse(user_id).all_group_ids:
            raise UserError(_(
                'Tickets can only be assigned to an IT Helpdesk agent or manager.'
            ))

    def _notify_assignment(self):
        for ticket in self:
            if ticket.user_id and ticket.user_id.partner_id:
                ticket.message_subscribe(partner_ids=ticket.user_id.partner_id.ids)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _check_agent(self):
        if not self.env.su and not self.env.user.has_group('KSW_helpdesk.group_helpdesk_agent'):
            raise UserError(_('Only IT Helpdesk agents can do this.'))

    def action_assign_to_me(self):
        self._check_agent()
        self.write({'user_id': self.env.user.id})

    def action_close(self):
        self._check_agent()
        closed_stage = self.env['helpdesk.ticket.stage'].search(
            [('is_closed', '=', True)], order='sequence', limit=1,
        )
        for ticket in self:
            ticket.write({
                'stage_id': closed_stage.id if closed_stage else ticket.stage_id.id,
                'close_date': fields.Datetime.now(),
                'closed_by': self.env.user.id,
                'kanban_state': 'done',
            })

    def action_reopen(self):
        self._check_agent()
        open_stage = self.env['helpdesk.ticket.stage'].search(
            [('is_closed', '=', False)], order='sequence', limit=1,
        )
        for ticket in self:
            ticket.write({
                'stage_id': open_stage.id if open_stage else ticket.stage_id.id,
                'close_date': False,
                'closed_by': False,
                'kanban_state': 'normal',
            })
