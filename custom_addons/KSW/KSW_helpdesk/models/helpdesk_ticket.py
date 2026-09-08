from markupsafe import Markup

from odoo import SUPERUSER_ID, _, api, fields, models
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
    attachment_ids = fields.Many2many(
        'ir.attachment', string='Attachments',
        help="Screenshots, error logs, or any other file that helps explain the issue.",
    )
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
        tickets._notify_new_ticket()
        tickets._notify_assignment()
        tickets._relink_orphan_attachments()
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
        was_closed = {ticket.id: ticket.is_closed for ticket in self}
        previous_assignee = {ticket.id: ticket.user_id.id for ticket in self}
        result = super().write(vals)
        if 'user_id' in vals:
            self._notify_assignment(previous_assignee)
        self._sync_close_state(was_closed)
        return result

    def _relink_orphan_attachments(self):
        # Files added via the Attachments tab before the ticket's first save
        # are uploaded with res_id=0 (the ticket doesn't exist yet). ir.attachment
        # treats a falsy res_id as "no linked document" and restricts access to
        # the creator only (see ir.attachment._check_access), so IT agents can't
        # see them unless we re-stamp res_id once the ticket has a real one.
        for ticket in self:
            orphans = ticket.attachment_ids.filtered(lambda a: not a.res_id)
            if orphans:
                orphans.sudo().write({'res_model': 'helpdesk.ticket', 'res_id': ticket.id})

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

    # ------------------------------------------------------------------
    # Notifications
    #
    # Everything goes through message_post(partner_ids=..., subtype_xmlid=
    # 'mail.mt_comment'), the house pattern in this codebase (see
    # KSW_annual_leave._notify_pending_approvers): it lands in the
    # recipient's Odoo inbox AND is emailed to them according to their own
    # notification preference, without forcing them to become a follower.
    # sudo() is used for the chatter write only - authorisation is already
    # settled by the time we get here, and a plain employee raising a ticket
    # may not otherwise create a mail.message naming IT Team partners
    # (CLAUDE.md gotcha #11).
    # ------------------------------------------------------------------
    def _helpdesk_agent_partners(self):
        """Partners of every active IT Team member, minus the acting user."""
        group = self.env.ref(
            'KSW_helpdesk.group_helpdesk_agent', raise_if_not_found=False)
        if not group:
            return self.env['res.partner']
        agents = self.env['res.users'].sudo().search([
            ('all_group_ids', 'in', group.id),
            ('share', '=', False),
            ('id', '!=', SUPERUSER_ID),
        ])
        return (agents - self.env.user).partner_id

    def _requester_partner(self):
        """The partner to notify for the employee the ticket was raised for.

        Their user account's partner when they have one, otherwise the HR
        work contact - an employee with no Odoo login still gets the email.
        """
        self.ensure_one()
        employee = self.employee_id.sudo()
        return employee.user_id.partner_id or employee.work_contact_id

    def _notify_new_ticket(self):
        """Tell the whole IT Team a ticket has come in."""
        agent_partners = self._helpdesk_agent_partners()
        for ticket in self:
            requester = ticket._requester_partner()
            if requester:
                # so the requester sees every reply on their own ticket
                ticket.sudo().message_subscribe(partner_ids=requester.ids)
            recipients = agent_partners - requester - ticket.user_id.partner_id
            if not recipients:
                continue
            ticket.sudo().message_post(
                body=Markup(
                    '<strong>&#127915; New %(type)s — %(ref)s</strong><br/>'
                    '<b>Subject:</b> %(subject)s<br/>'
                    '<b>Reported by:</b> %(employee)s%(department)s<br/>'
                    '<b>Category:</b> %(category)s<br/>'
                    '<b>Priority:</b> %(priority)s<br/>'
                    'A new ticket is waiting to be picked up by the IT Team.'
                ) % {
                    'type': ticket._ticket_type_label(),
                    'ref': ticket.ticket_ref,
                    'subject': ticket.name,
                    'employee': ticket.employee_id.display_name,
                    'department': (
                        ' (%s)' % ticket.caller_department_id.display_name
                        if ticket.caller_department_id else ''
                    ),
                    'category': ticket.category_id.display_name,
                    'priority': dict(
                        ticket._fields['priority'].selection).get(ticket.priority, ''),
                },
                partner_ids=recipients.ids,
                subtype_xmlid='mail.mt_comment',
            )

    def _notify_assignment(self, previous_assignee=None):
        """Subscribe the assignee, and notify them when they are new to it.

        ``previous_assignee`` maps ticket id -> the user_id before the write,
        so re-saving a ticket without changing its assignee stays silent.
        """
        for ticket in self:
            partner = ticket.user_id.partner_id
            if not partner:
                continue
            ticket.sudo().message_subscribe(partner_ids=partner.ids)
            if previous_assignee and previous_assignee.get(ticket.id) == ticket.user_id.id:
                continue
            if ticket.user_id == self.env.user:
                continue  # "Assign to me" - no point notifying yourself
            ticket.sudo().message_post(
                body=Markup(
                    '<strong>&#128100; Ticket assigned to you — %(ref)s</strong><br/>'
                    '<b>Subject:</b> %(subject)s<br/>'
                    '<b>Reported by:</b> %(employee)s<br/>'
                    '<b>Priority:</b> %(priority)s'
                ) % {
                    'ref': ticket.ticket_ref,
                    'subject': ticket.name,
                    'employee': ticket.employee_id.display_name,
                    'priority': dict(
                        ticket._fields['priority'].selection).get(ticket.priority, ''),
                },
                partner_ids=partner.ids,
                subtype_xmlid='mail.mt_comment',
            )

    def _ticket_type_label(self):
        self.ensure_one()
        return dict(self._fields['ticket_type'].selection).get(self.ticket_type, '')

    def _sync_close_state(self, was_closed):
        """Stamp and notify on the close/reopen *transition*, from any route.

        A ticket is closed by the Close button, by dragging its kanban card
        into the Closed column, or by any RPC write on stage_id. Hooking the
        transition rather than the one button we were shown is the same rule
        as CLAUDE.md gotcha #37.
        """
        newly_closed = self.filtered(lambda t: t.is_closed and not was_closed.get(t.id))
        newly_reopened = self.filtered(
            lambda t: not t.is_closed and was_closed.get(t.id))
        if newly_closed:
            unstamped = newly_closed.filtered(lambda t: not t.close_date)
            if unstamped:
                # super() so this second write does not re-enter the guard
                # or re-trigger the transition detection
                super(HelpdeskTicket, unstamped).write({
                    'close_date': fields.Datetime.now(),
                    'closed_by': self.env.user.id,
                })
            newly_closed._notify_ticket_closed()
        if newly_reopened:
            super(HelpdeskTicket, newly_reopened).write({
                'close_date': False,
                'closed_by': False,
            })

    def _notify_ticket_closed(self):
        """Tell the person who raised the ticket that it has been resolved."""
        for ticket in self:
            body = Markup(
                '<strong>&#9989; Ticket closed — %(ref)s</strong><br/>'
                '<b>Subject:</b> %(subject)s<br/>'
                '<b>Closed by:</b> %(closed_by)s<br/>'
                'If the issue is not fully resolved, reply here and the IT '
                'Team will follow up.'
            ) % {
                'ref': ticket.ticket_ref,
                'subject': ticket.name,
                'closed_by': (ticket.closed_by or self.env.user).display_name,
            }
            partner = ticket._requester_partner()
            if partner:
                ticket.sudo().message_post(
                    body=body,
                    partner_ids=partner.ids,
                    subtype_xmlid='mail.mt_comment',
                )
            else:
                # No partner to notify at all: log it on the thread and fall
                # back to the plain email template addressed to work_email.
                ticket.sudo().message_post(body=body, subtype_xmlid='mail.mt_note')
                ticket._send_close_email()

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
        """Move to the closing stage. The stamping and the notification to
        the requester are done by _sync_close_state() in write(), so dragging
        the card into the Closed column behaves exactly the same."""
        self._check_agent()
        closed_stage = self.env['helpdesk.ticket.stage'].search(
            [('is_closed', '=', True)], order='sequence', limit=1,
        )
        if not closed_stage:
            raise UserError(_(
                'No closing stage is configured. Tick "Is Closed" on the '
                'stage that ends a ticket first.'
            ))
        self.write({'stage_id': closed_stage.id, 'kanban_state': 'done'})

    def _send_close_email(self):
        """Fallback for a requester with no partner: the plain email template.

        Reachable in practice - hr.employee.create() always builds a work
        contact, but hr.employee._remove_work_contact_id() clears it again
        when that user account is moved to another employee record.
        """
        template = self.env.ref(
            'KSW_helpdesk.mail_template_ticket_closed', raise_if_not_found=False)
        if not template:
            return
        force_send = self.env.context.get('mail_notify_force_send', True)
        for ticket in self:
            if ticket.caller_email:
                template.sudo().send_mail(ticket.id, force_send=force_send)

    def action_reopen(self):
        self._check_agent()
        open_stage = self.env['helpdesk.ticket.stage'].search(
            [('is_closed', '=', False)], order='sequence', limit=1,
        )
        if not open_stage:
            raise UserError(_('No open stage is configured.'))
        self.write({'stage_id': open_stage.id, 'kanban_state': 'normal'})
