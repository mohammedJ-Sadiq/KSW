import logging
import re

from markupsafe import Markup

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# The three approver tiers, reused rather than reinvented (see the module
# README / brain note): the HR step is the payroll Officer tier that already
# owns "Issue Revision"; the GM step is routed to the requester's own
# department GM exactly as the annual-leave chain does; the accounting step
# is the existing Accounting Approver group.
HR_GROUP = 'om_hr_payroll.group_hr_payroll_user'
ACC_GROUP = 'KSW_annual_leave.group_annual_leave_acc'
GM_GROUP = 'KSW_annual_leave.group_annual_leave_gm'


class KswPayslipRevisionRequest(models.Model):
    """An employee's complaint that a confirmed payslip paid them short.

    Structurally a leave request: its own document, its own chatter, its own
    approval chain, its own notifications.  The payslip revision itself
    (``hr.payslip.x_is_revision``) is the *outcome* of the HR step, not the
    request — the same way an approved time-off request is what finally
    writes the attendance.

        draft → pending_hr → pending_gm → pending_acc → paid

    Refusal, with a reason, is available at every pending step; the GM may
    also return the request to HR when the *figure* needs reworking rather
    than the complaint being wrong.
    """

    _name = 'ksw.payslip.revision.request'
    _description = 'Payslip Revision Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    _STATES = [
        ('draft', 'Draft'),
        ('pending_hr', 'Pending HR Review'),
        ('pending_gm', 'Pending GM Approval'),
        ('pending_acc', 'Pending Accounting Payment'),
        ('paid', 'Paid'),
        ('refused', 'Refused'),
        ('cancelled', 'Cancelled'),
    ]

    # Which group is being waited on at each step, for notifications and for
    # the "Waiting for My Action" filter.  ``department_gm`` routes to one
    # person rather than a whole group.
    _STEP_CONFIG = {
        'pending_hr': {'group': HR_GROUP, 'label': 'HR Review'},
        'pending_gm': {'department_gm': True, 'label': 'GM Approval'},
        'pending_acc': {'group': ACC_GROUP, 'label': 'Accounting Payment'},
    }

    name = fields.Char(
        string='Reference', required=True, copy=False, readonly=True,
        default=lambda self: _('New'), index=True,
    )
    state = fields.Selection(
        _STATES, string='Status', default='draft', required=True,
        readonly=True, copy=False, index=True, tracking=True,
    )

    # ------------------------------------------------------------------
    # What is being complained about
    # ------------------------------------------------------------------
    payslip_id = fields.Many2one(
        'hr.payslip', string='Payslip', required=True, ondelete='cascade',
        tracking=True,
        domain="[('state', '=', 'done'), ('credit_note', '=', False),"
               " ('x_is_revision', '=', False)]",
        help='The confirmed payslip the employee believes was short.',
    )
    employee_id = fields.Many2one(
        'hr.employee', string='Employee', required=True, readonly=True,
        index=True, tracking=True,
    )
    department_id = fields.Many2one(
        'hr.department', string='Department', readonly=True, index=True,
    )
    company_id = fields.Many2one(
        'res.company', string='Company', readonly=True,
        default=lambda self: self.env.company,
    )
    date_from = fields.Date(string='Period From', readonly=True)
    date_to = fields.Date(string='Period To', readonly=True)
    request_date = fields.Date(
        string='Request Date', readonly=True, copy=False,
        default=fields.Date.context_today,
    )

    reason = fields.Text(
        string='What is wrong?', required=True, tracking=True,
        help='Describe what the employee believes is missing or wrong in '
             'this payslip.',
    )
    claimed_amount = fields.Float(
        string='Amount Claimed', digits=(16, 2),
        help='Optional — what the employee believes they are still owed. '
             'Indicative only: the figure actually paid is whatever the '
             'revision computes.',
    )
    attachment_ids = fields.Many2many(
        'ir.attachment', 'ksw_revision_request_attachment_rel',
        'request_id', 'attachment_id', string='Supporting Documents',
        help='The signed complaint form and anything supporting it.',
    )

    # ------------------------------------------------------------------
    # Step stamps
    # ------------------------------------------------------------------
    hr_comment = fields.Text(
        string='HR Findings', tracking=True,
        help="HR's conclusion after checking the complaint. Required "
             'before the request can be accepted or refused.',
    )
    hr_user_id = fields.Many2one('res.users', string='Reviewed By',
                                 readonly=True, copy=False)
    hr_date = fields.Datetime(string='Reviewed On', readonly=True, copy=False)

    gm_comment = fields.Text(string='GM Comment', tracking=True)
    gm_user_id = fields.Many2one('res.users', string='Approved By (GM)',
                                 readonly=True, copy=False)
    gm_date = fields.Datetime(string='Approved On (GM)', readonly=True,
                              copy=False)

    acc_user_id = fields.Many2one('res.users', string='Paid By',
                                  readonly=True, copy=False)
    acc_date = fields.Datetime(string='Paid On', readonly=True, copy=False)

    refuse_reason = fields.Text(string='Refusal Reason', readonly=True,
                                copy=False, tracking=True)
    refused_by_id = fields.Many2one('res.users', string='Refused By',
                                    readonly=True, copy=False)

    # ------------------------------------------------------------------
    # The revision payslip produced by the HR step
    # ------------------------------------------------------------------
    revision_payslip_id = fields.Many2one(
        'hr.payslip', string='Revision Payslip', readonly=True, copy=False,
        help='The revision HR issued for this complaint. Its NET is the '
             'difference payable.',
    )
    revision_state = fields.Selection(
        related='revision_payslip_id.state', string='Revision Status',
        readonly=True,
    )
    payslip_run_id = fields.Many2one(
        'hr.payslip.run', string='Payment Batch', readonly=True, copy=False,
        help='The single-employee batch the bank file is generated from.',
    )

    # Money figures are sudo computes on purpose: the GM and the accounting
    # approver hold no hr.payslip access of their own, and a related= would
    # drag the payslip's own field groups= along with it (pitfall #5 / the
    # "related fields inherit groups=" note).  Nothing here is stored — a
    # revision can still be recomputed right up to confirmation.
    original_net = fields.Float(
        string='Originally Paid', compute='_compute_amounts',
        compute_sudo=True, digits=(16, 2),
    )
    deserved_net = fields.Float(
        string='Deserved Net', compute='_compute_amounts',
        compute_sudo=True, digits=(16, 2),
    )
    difference_amount = fields.Float(
        string='Difference Payable', compute='_compute_amounts',
        compute_sudo=True, digits=(16, 2),
    )

    payment_method = fields.Selection(
        [('bank', 'Bank Transfer'), ('cash', 'Cash')],
        string='Payment Method', tracking=True, copy=False,
        help='How accounting settled the difference.',
    )
    payment_reference = fields.Char(string='Payment Reference', copy=False)
    payment_date = fields.Date(string='Payment Date', readonly=True,
                               copy=False)

    # ------------------------------------------------------------------
    # Per-user gate fields — every one needs depends_context('uid')
    # (pitfall #14), or the ORM cache hands the first user's answer to
    # everybody else in the same request.
    # ------------------------------------------------------------------
    can_edit = fields.Boolean(compute='_compute_permissions',
                              compute_sudo=False)
    can_submit = fields.Boolean(compute='_compute_permissions',
                                compute_sudo=False)
    can_hr_act = fields.Boolean(compute='_compute_permissions',
                                compute_sudo=False)
    can_gm_act = fields.Boolean(compute='_compute_permissions',
                                compute_sudo=False)
    can_acc_act = fields.Boolean(compute='_compute_permissions',
                                 compute_sudo=False)
    can_cancel = fields.Boolean(compute='_compute_permissions',
                                compute_sudo=False)
    is_pending_my_action = fields.Boolean(
        compute='_compute_permissions', compute_sudo=False,
        search='_search_is_pending_my_action',
    )

    # ==================================================================
    # Computes
    # ==================================================================

    @api.depends('payslip_id', 'revision_payslip_id',
                 'revision_payslip_id.line_ids.total')
    def _compute_amounts(self):
        Payslip = self.env['hr.payslip']
        for req in self:
            source = req.payslip_id.sudo()
            revision = req.revision_payslip_id.sudo()
            req.original_net = (
                Payslip._get_net_total(source) if source else 0.0)
            if revision:
                req.difference_amount = Payslip._get_net_total(revision)
                req.deserved_net = revision.x_deserved_net
            else:
                req.difference_amount = 0.0
                req.deserved_net = 0.0

    @api.depends_context('uid')
    @api.depends('state', 'employee_id', 'payslip_id')
    def _compute_permissions(self):
        user = self.env.user
        is_hr = user.has_group(HR_GROUP)
        is_acc = user.has_group(ACC_GROUP)
        for req in self:
            # Identity reads go through sudo(): a plain employee holding the
            # Employees privilege may read exactly one hr.employee row —
            # their own — so reading their manager here would crash the form
            # before any button could be pressed (pitfall #69).
            # On a record that has not been saved yet ``employee_id`` is
            # still empty — it is stamped by create() — so fall back to the
            # payslip's own employee, or the picker on a brand-new form
            # would open read-only and there would be no way to choose a
            # payslip at all.
            employee = (req.employee_id or req.payslip_id.employee_id).sudo()
            is_owner = bool(employee) and employee.user_id == user
            is_manager = (
                bool(employee.parent_id) and employee.parent_id.user_id == user)
            is_gm = req._is_department_gm(user)

            req.can_edit = req.state == 'draft' and (
                not employee or is_owner or is_manager or is_hr)
            req.can_submit = req.can_edit
            req.can_cancel = req.state in ('draft', 'pending_hr') and (
                is_owner or is_manager or is_hr)
            req.can_hr_act = req.state == 'pending_hr' and is_hr
            req.can_gm_act = req.state == 'pending_gm' and is_gm
            req.can_acc_act = req.state == 'pending_acc' and is_acc
            req.is_pending_my_action = (
                req.can_hr_act or req.can_gm_act or req.can_acc_act)

    def _search_is_pending_my_action(self, operator, value):
        """Records waiting on the current user.

        Odoo 19 rewrites ``=``/``!=`` on a boolean into ``in``/``not in``
        with an OrderedSet value *before* a ``search=`` method is called, so
        branch on the operator and never on the value's type (pitfall #24).
        """
        if operator in ('in', 'not in'):
            positive_wanted = (operator == 'in') == any(value)
        elif operator in ('=', '!='):
            positive_wanted = (operator == '=') == bool(value)
        else:
            return NotImplemented

        user = self.env.user
        states = []
        if user.has_group(HR_GROUP):
            states.append('pending_hr')
        if user.has_group(ACC_GROUP):
            states.append('pending_acc')

        gm_domain = []
        if user.has_group(GM_GROUP):
            gm_domain = [
                '&', ('state', '=', 'pending_gm'),
                '|',
                ('employee_id.department_id.x_effective_gm_id.user_id',
                 '=', user.id),
                '&', ('employee_id.department_id', '=', False),
                ('company_id.x_default_gm_id.user_id', '=', user.id),
            ]

        if states and gm_domain:
            domain = ['|', ('state', 'in', states)] + gm_domain
        elif states:
            domain = [('state', 'in', states)]
        elif gm_domain:
            domain = gm_domain
        else:
            # Nothing can ever be pending this user's action.  `[]` would
            # mean "match everything" (pitfall #24), so say so explicitly.
            domain = [('id', 'in', [])]

        if positive_wanted:
            return domain
        return ['!'] + domain

    # ==================================================================
    # Approver resolution
    # ==================================================================

    def _department_gm_user(self):
        """The GM who must clear this request's GM step.

        Same resolution as the annual-leave chain: the requester's own
        department GM, falling back to the company default for the ~100
        employees who have no department at all.  sudo() throughout — this
        is an identity read, not a scope one.
        """
        self.ensure_one()
        employee = self.employee_id.sudo()
        gm = employee.department_id.x_effective_gm_id
        if not gm:
            gm = (self.company_id or self.env.company).sudo().x_default_gm_id
        return gm.sudo().user_id

    def _is_department_gm(self, user):
        self.ensure_one()
        if not user.has_group(GM_GROUP):
            return False
        return self._department_gm_user() == user

    # ==================================================================
    # Create / write guards
    # ==================================================================

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'ksw.payslip.revision.request') or _('New')
            payslip = self.env['hr.payslip'].sudo().browse(
                vals.get('payslip_id'))
            self._check_can_file(payslip)
            vals.update(self._defaults_from_payslip(payslip))
        requests = super().create(vals_list)
        for req in requests:
            req.sudo().message_post(
                body=Markup(
                    '<strong>&#129534; Revision request created</strong><br/>'
                    '<b>Payslip:</b> %(slip)s<br/>'
                    '<b>Filed by:</b> %(user)s'
                ) % {
                    'slip': payslip_name(req.payslip_id.sudo()),
                    'user': self.env.user.name,
                },
                subtype_xmlid='mail.mt_note',
            )
        return requests

    @api.onchange('payslip_id')
    def _onchange_payslip_id(self):
        """Show the header before the record is saved.

        ``employee_id`` / ``department_id`` / the period are plain **stored**
        fields written by ``create()``, deliberately not computes or related
        fields: the request has to keep stating which employee and which
        period the complaint was about long after the payslip has moved on
        (pitfall #50). The cost of that choice is that nothing fills them on
        screen, so the form opened from a payslip showed an empty — and
        `required` — Employee, which the client then refuses to save.

        This fills them for display only. ``create()`` stays the authority
        and re-derives every one of them from the payslip it is actually
        given, so an employee who edits the values the client sent gains
        nothing.
        """
        for req in self:
            vals = req._defaults_from_payslip(req.payslip_id.sudo())
            req.employee_id = vals.get('employee_id', False)
            req.department_id = vals.get('department_id', False)
            req.date_from = vals.get('date_from', False)
            req.date_to = vals.get('date_to', False)
            if vals.get('company_id'):
                req.company_id = vals['company_id']

    @api.model
    def _defaults_from_payslip(self, payslip):
        """Header fields copied off the payslip at filing time.

        Plain stored fields, written once — not computes or related fields:
        the request must still state which employee and which period it was
        about after the payslip it points at has moved on (pitfall #50).
        """
        if not payslip:
            return {}
        employee = payslip.employee_id
        return {
            'employee_id': employee.id,
            'department_id': employee.department_id.id,
            'company_id': payslip.company_id.id,
            'date_from': payslip.date_from,
            'date_to': payslip.date_to,
        }

    @api.model
    def _check_can_file(self, payslip, exclude=None):
        """Who may open a complaint, and against what.

        The employee themselves, their direct manager, or the payroll team
        on behalf of an employee who brought the paperwork to the desk.
        """
        if self.env.su:
            return
        if not payslip:
            raise UserError(_('A revision request must name a payslip.'))
        if payslip.state != 'done':
            raise UserError(_(
                'Only a confirmed payslip can be disputed. "%s" has not '
                'been confirmed yet.'
            ) % payslip_name(payslip))
        if payslip.x_is_revision:
            raise UserError(_(
                'This payslip is itself a revision. Dispute the original '
                'payslip instead.'))
        if payslip.credit_note:
            raise UserError(_('A refund payslip cannot be disputed.'))

        domain = [
            ('payslip_id', '=', payslip.id),
            ('state', 'not in', ('refused', 'cancelled')),
        ]
        if exclude:
            domain.append(('id', 'not in', exclude.ids))
        open_request = self.sudo().search(domain, limit=1)
        if open_request:
            raise UserError(_(
                'A revision request is already open for this payslip '
                '(%(ref)s, %(state)s). Follow that one up instead of '
                'filing a second.',
                ref=open_request.name,
                state=dict(self._STATES).get(open_request.state),
            ))

        user = self.env.user
        employee = payslip.employee_id.sudo()
        if user.has_group(HR_GROUP):
            return
        if employee.user_id == user:
            return
        if employee.parent_id.user_id == user:
            return
        raise UserError(_(
            'You may only request a revision of your own payslip, or of a '
            'payslip belonging to an employee who reports to you.'))

    # Which fields each audience may still write, by the step the request
    # is sitting on.  Everything else — the state itself, every stamp — is
    # written by the action methods through sudo(), which is the only route
    # that has checked authority first.
    _DRAFT_WRITABLE = {
        'payslip_id', 'reason', 'claimed_amount', 'attachment_ids',
    }
    _HR_WRITABLE = {'hr_comment', 'attachment_ids'}
    _GM_WRITABLE = {'gm_comment'}
    _ACC_WRITABLE = {'payment_method', 'payment_reference'}

    def _allowed_write_fields(self):
        """The fields the current user may write on this record right now.

        A whitelist, not a state check: write access on the document would
        otherwise let the employee set ``state`` to ``paid`` over RPC and
        skip all three approvers (pitfall #15).
        """
        self.ensure_one()
        employee = self.employee_id.sudo()
        user = self.env.user
        is_hr = user.has_group(HR_GROUP)
        is_filer = (
            employee.user_id == user
            or employee.parent_id.user_id == user
            or is_hr
        )
        if self.state == 'draft' and is_filer:
            return set(self._DRAFT_WRITABLE)
        if self.state == 'pending_hr' and is_hr:
            return set(self._HR_WRITABLE)
        if self.state == 'pending_gm' and self._is_department_gm(user):
            return set(self._GM_WRITABLE)
        if self.state == 'pending_acc' and user.has_group(ACC_GROUP):
            return set(self._ACC_WRITABLE)
        return set()

    def write(self, vals):
        """Guard which fields move, not just which records.

        ``env.su`` is exempt, or crons and migrations break (pitfall #37),
        and so is the chatter bookkeeping — following a document is not a
        business edit.
        """
        if not self.env.su:
            business = set(vals) - self._non_business_fields()
            if business:
                for req in self:
                    forbidden = business - req._allowed_write_fields()
                    if forbidden:
                        raise UserError(_(
                            'You cannot change %(fields)s on a request in '
                            'state "%(state)s". Only the approver the '
                            'request is currently waiting on may edit it, '
                            'and only their own part of it.',
                            fields=', '.join(sorted(
                                self._fields[f].string
                                if f in self._fields else f
                                for f in forbidden)),
                            state=dict(self._STATES).get(
                                req.state, req.state),
                        ))
        res = super().write(vals)
        # The header block is stamped at filing time and must keep stating
        # which employee and period the complaint was about even after the
        # payslip has moved on (pitfall #50) — but while the request is
        # still a draft, repointing it must re-derive that block or it goes
        # stale against its own payslip.
        if 'payslip_id' in vals:
            for req in self:
                payslip = req.payslip_id.sudo()
                if not self.env.su:
                    req._check_can_file(payslip, exclude=req)
                super(KswPayslipRevisionRequest, req).write(
                    req._defaults_from_payslip(payslip))
        return res

    @api.model
    def _non_business_fields(self):
        """mail.thread / activity bookkeeping — following or reading a
        document is not a business edit, so it is never gated."""
        return {
            'message_follower_ids', 'message_ids', 'activity_ids',
            'message_main_attachment_id', 'message_partner_ids',
            'activity_user_id', 'activity_state', 'activity_date_deadline',
            'activity_summary', 'activity_type_id', 'activity_type_icon',
            'activity_exception_decoration', 'activity_exception_icon',
            'website_message_ids', 'message_has_error', 'message_needaction',
            'message_unread', 'message_attachment_count', 'message_is_follower',
        }

    def unlink(self):
        if not self.env.su:
            for req in self:
                if req.state != 'draft':
                    raise UserError(_(
                        'Only a draft request can be deleted. Use Cancel '
                        'instead, so the trail is kept.'))
        return super().unlink()

    # ==================================================================
    # Step 0 — the employee submits
    # ==================================================================

    def action_submit(self):
        for req in self:
            if req.state != 'draft':
                raise UserError(_('Only a draft request can be submitted.'))
            if not req.can_submit:
                raise UserError(_(
                    'You may only submit your own request, or one you filed '
                    'for an employee who reports to you.'))
            if not (req.reason or '').strip():
                raise UserError(_(
                    'Describe what is wrong with the payslip before '
                    'submitting.'))
            req.sudo().write({'state': 'pending_hr'})
            req._post_step_note(
                '📨 Submitted', _('Sent to HR for review.'))
            req._notify_pending_approvers('pending_hr')
        return True

    # ==================================================================
    # Step 1 — HR checks the complaint and issues the revision
    # ==================================================================

    def action_hr_accept(self):
        """HR agrees with the complaint: issue the revision payslip.

        The revision is left in **draft** so the GM approves a figure that
        was actually computed rather than a promise, and so accounting can
        still recompute it right before paying — confirming a payslip is a
        recompute, so a file exported from a draft can stop matching.
        """
        for req in self:
            req._check_step('pending_hr', HR_GROUP, _(
                'Only the payroll team may review a revision request.'))
            if not (req.hr_comment or '').strip():
                raise UserError(_(
                    'Record your findings in "HR Findings" before accepting '
                    'the request — the GM approves on the strength of that '
                    'note.'))
            revision = req._issue_revision_payslip()
            req.sudo().write({
                'state': 'pending_gm',
                'revision_payslip_id': revision.id,
                'hr_user_id': self.env.uid,
                'hr_date': fields.Datetime.now(),
            })
            req._post_step_note(
                '✅ Accepted by HR',
                _('Revision %(slip)s issued — difference payable '
                  '%(amount).2f SAR.',
                  slip=payslip_name(revision),
                  amount=req.difference_amount),
            )
            req._notify_pending_approvers('pending_gm')
        return True

    def _issue_revision_payslip(self):
        """Build (or reuse) the revision payslip for the disputed period.

        Delegates to the existing ``hr.payslip`` machinery rather than
        rebuilding it: ``_create_revision_payslip`` is what knows about
        PRIOR_NET, the frozen ``KSW_DEDP_`` installments and the one-time
        inputs that must be re-stated exactly once.
        """
        self.ensure_one()
        source = self.payslip_id.sudo()
        if self.revision_payslip_id and self.revision_payslip_id.state in (
                'draft', 'verify'):
            return self.revision_payslip_id.sudo()

        # Reopen an open revision for the period rather than forking it —
        # same rule action_issue_revision applies.
        existing = source.search(
            source._overlapping_slips_domain(states=('draft', 'verify'))
            + [('x_is_revision', '=', True)], limit=1)
        if existing:
            return existing.sudo()
        return source._create_revision_payslip()

    def action_hr_refuse(self):
        """HR checked the complaint and it does not hold."""
        self.ensure_one()
        self._check_step('pending_hr', HR_GROUP, _(
            'Only the payroll team may review a revision request.'))
        return self._open_reason_wizard('refuse')

    # ==================================================================
    # Step 2 — the GM approves the figure
    # ==================================================================

    def action_gm_approve(self):
        for req in self:
            if req.state != 'pending_gm':
                raise UserError(_(
                    'This request is not waiting for GM approval.'))
            req._check_gm()
            revision = req.revision_payslip_id.sudo()
            if not revision or revision.state == 'cancel':
                raise UserError(_(
                    'The revision payslip for this request no longer '
                    'exists. Ask HR to re-issue it.'))
            if req.difference_amount <= 0:
                raise UserError(_(
                    'The revision shows nothing further is owed '
                    '(%(amount).2f SAR). Refuse the request instead of '
                    'approving a payment of zero.',
                    amount=req.difference_amount))
            req.sudo().write({
                'state': 'pending_acc',
                'gm_user_id': self.env.uid,
                'gm_date': fields.Datetime.now(),
            })
            req._post_step_note(
                '✅ Approved by GM',
                _('Sent to accounting to pay %(amount).2f SAR.',
                  amount=req.difference_amount),
            )
            req._notify_pending_approvers('pending_acc')
        return True

    def action_gm_refuse(self):
        self.ensure_one()
        if self.state != 'pending_gm':
            raise UserError(_('This request is not waiting for GM approval.'))
        self._check_gm()
        return self._open_reason_wizard('refuse')

    def action_gm_return_to_hr(self):
        """Send the request back to HR with the figure to rework.

        Not a refusal: the complaint may well be justified and the revision
        simply wrong.  The revision payslip is cancelled on the way back so
        HR rebuilds it rather than editing a document the GM already saw.
        """
        self.ensure_one()
        if self.state != 'pending_gm':
            raise UserError(_('This request is not waiting for GM approval.'))
        self._check_gm()
        return self._open_reason_wizard('return')

    def _apply_return_to_hr(self, reason):
        self.ensure_one()
        revision = self.revision_payslip_id.sudo()
        if revision and revision.state in ('draft', 'verify'):
            revision.action_payslip_cancel()
        self.sudo().write({
            'state': 'pending_hr',
            'revision_payslip_id': False,
            'gm_user_id': False,
            'gm_date': False,
        })
        self._post_step_note(
            '↩️ Returned to HR',
            _('Returned by %(user)s: %(reason)s',
              user=self.env.user.name, reason=reason),
        )
        self._notify_pending_approvers('pending_hr')

    # ==================================================================
    # Step 3 — accounting confirms the revision and pays
    # ==================================================================

    def action_acc_pay(self):
        """Confirm the revision payslip, then record the payment.

        Confirming is deliberately here and not earlier: ``action_payslip_
        done`` recomputes the sheet, so the figure is fixed at the moment
        the money is committed and the bank file is generated from a
        payslip that can no longer move.
        """
        for req in self:
            req._check_step('pending_acc', ACC_GROUP, _(
                'Only the accounting team may pay a revision request.'))
            if not req.payment_method:
                raise UserError(_(
                    'Choose how the difference is being paid — bank '
                    'transfer or cash — before confirming.'))
            revision = req.revision_payslip_id.sudo()
            if not revision:
                raise UserError(_(
                    'This request has no revision payslip to pay.'))
            if revision.state not in ('done',):
                revision.action_payslip_done()
                # action_payslip_done refuses an over-paid revision rather
                # than confirming a negative net; say so plainly instead of
                # marking the request paid on a payslip that never went done.
                revision.invalidate_recordset(['state'])
                if revision.state != 'done':
                    raise UserError(_(
                        'The revision payslip could not be confirmed. Open '
                        '%(slip)s and check it — the period may now show '
                        'the employee was over-paid.',
                        slip=payslip_name(revision)))
            req.sudo().write({
                'state': 'paid',
                'acc_user_id': self.env.uid,
                'acc_date': fields.Datetime.now(),
                'payment_date': fields.Date.context_today(req),
            })
            req._post_step_note(
                '💵 Paid',
                _('%(amount).2f SAR paid by %(method)s.',
                  amount=req.difference_amount,
                  method=dict(
                      req._fields['payment_method'].selection
                  ).get(req.payment_method, req.payment_method)),
            )
            req._notify_employee_paid()
        return True

    def action_acc_refuse(self):
        self.ensure_one()
        self._check_step('pending_acc', ACC_GROUP, _(
            'Only the accounting team may act on this step.'))
        return self._open_reason_wizard('refuse')

    # ------------------------------------------------------------------
    # Bank file for this employee alone
    # ------------------------------------------------------------------

    def action_export_bank_excel(self):
        """The Excel bank file for this one revision."""
        return self._generate_bank_file('all_excel')

    def action_export_bank_txt(self):
        """The bank transfer text file for this one revision."""
        return self._generate_bank_file('all_txt')

    def _generate_bank_file(self, mode):
        """Generate the Excel / TXT bank file for this revision alone.

        The export machinery is batch-shaped — it groups slips by paying
        bank account and writes one sheet per bank — so rather than fork a
        second implementation, the revision goes into a batch of one and
        the existing wizard runs over it.  With one employee there is
        exactly one bank, so "all banks" yields exactly one file and the
        mode dialog has nothing to ask.

        It runs under ``sudo()`` on purpose, and that is the whole reason
        this is a direct action rather than a link to the wizard: the
        export reads wages and bank details that carry field-level
        ``groups=`` (pitfall #5), and the wizard's own ACL is the payroll
        Officer tier.  Accounting is authorised for *this* payment — the
        GM has signed it — without being handed the company's payroll
        export.  Authority is checked before the sudo, never after.
        """
        self.ensure_one()
        if not self.env.su and not (
                self.env.user.has_group(ACC_GROUP)
                or self.env.user.has_group(HR_GROUP)):
            raise UserError(_(
                'Only the accounting or payroll team may generate the bank '
                'file for a revision.'))
        if self.state not in ('pending_acc', 'paid'):
            raise UserError(_(
                'The bank file can only be generated once the GM has '
                'approved this request.'))
        revision = self.revision_payslip_id.sudo()
        if not revision:
            raise UserError(_('This request has no revision payslip.'))
        if revision.state != 'done':
            raise UserError(_(
                'Confirm the payment first — "Confirm & Pay" marks the '
                'revision payslip done, which fixes the figure the file '
                'will carry. A file exported before that can stop matching '
                'what is finally paid.'))

        run = self._ensure_payment_batch()
        wizard = self.env['ksw.bank.file.export.wizard'].sudo().create({
            'payslip_run_id': run.id,
            'export_mode': mode,
            'value_date': self.payment_date or fields.Date.context_today(self),
        })
        action = wizard.action_export()
        return self._file_export_on_request(action, mode)

    def _file_export_on_request(self, action, mode):
        """Re-home the generated attachment onto this request.

        The wizard files its output against the payslip batch, and the
        ``/web/content`` download route checks the reader's access to
        whatever the attachment points at — which for an accounting user
        is a batch they cannot open, so the link 403s.  Moving it onto the
        request both fixes that and leaves the exact file that went to the
        bank filed on the document it paid (pitfall: a display that needs
        access to a foreign model).
        """
        self.ensure_one()
        url = (action or {}).get('url') or ''
        match = re.search(r'/web/content/(\d+)', url)
        if not match:
            return action
        attachment = self.env['ir.attachment'].sudo().browse(int(match.group(1)))
        attachment.write({
            'res_model': self._name,
            'res_id': self.id,
        })
        self.sudo().write({'attachment_ids': [(4, attachment.id)]})
        self._post_step_note(
            '📄 Bank file generated',
            _('%(name)s generated by %(user)s.',
              name=attachment.name, user=self.env.user.name),
        )
        return action

    def _ensure_payment_batch(self):
        """The single-slip ``hr.payslip.run`` the file is generated from.

        Also the artifact accounting looks the payment up in later: a
        revision paid on its own otherwise belongs to no run at all.
        """
        self.ensure_one()
        revision = self.revision_payslip_id.sudo()
        run = self.payslip_run_id.sudo()
        if not run:
            run = self.env['hr.payslip.run'].sudo().create({
                # No em dashes: this name becomes the bank filename via
                # the export wizard's _batch_label().
                'name': _('Salary Revision %(ref)s - %(employee)s',
                          ref=self.name,
                          employee=self.employee_id.sudo().name),
                'date_start': self.date_from,
                'date_end': self.date_to,
            })
            self.sudo().write({'payslip_run_id': run.id})
        old_run = revision.payslip_run_id
        if old_run != run:
            revision.write({'payslip_run_id': run.id})
            # A revision is created standalone, so this is normally a
            # no-op; if somebody had filed it into a monthly batch, that
            # batch's per-bank totals are now stale.
            if old_run:
                old_run.sudo()._refresh_bank_totals()
        # A straight write, never done_payslip_run(): that confirms every
        # slip in the batch, and this one is already done — re-confirming a
        # done payslip recomputes it and silently drops the deduction
        # inputs it already settled (pitfall #49).
        if run.state != 'done':
            run.write({'state': 'done'})
        run._refresh_bank_totals()
        return run

    # ==================================================================
    # Cancel / refuse
    # ==================================================================

    def action_cancel(self):
        for req in self:
            if not req.can_cancel:
                raise UserError(_(
                    'This request can no longer be cancelled — HR has '
                    'already acted on it.'))
            req.sudo().write({'state': 'cancelled'})
            req._post_step_note(
                '🚫 Cancelled',
                _('Withdrawn by %s.') % self.env.user.name)
        return True

    def action_reset_to_draft(self):
        """Put a refused or cancelled request back in the employee's hands."""
        for req in self:
            if req.state not in ('refused', 'cancelled'):
                raise UserError(_(
                    'Only a refused or cancelled request can be reopened.'))
            if not self.env.su:
                employee = req.employee_id.sudo()
                if not (self.env.user.has_group(HR_GROUP)
                        or employee.user_id == self.env.user
                        or employee.parent_id.user_id == self.env.user):
                    raise UserError(_(
                        'You may only reopen your own request.'))
            # Another complaint may have been filed and accepted in the
            # meantime; reopening must not produce two open requests for
            # the same payslip.
            if not self.env.su:
                req._check_can_file(req.payslip_id.sudo(), exclude=req)
            req.sudo().write({
                'state': 'draft',
                'refuse_reason': False,
                'refused_by_id': False,
            })
            req._post_step_note(
                '🔄 Reopened',
                _('Put back in draft by %s.') % self.env.user.name)
        return True

    def _apply_refusal(self, reason):
        self.ensure_one()
        revision = self.revision_payslip_id.sudo()
        if revision and revision.state in ('draft', 'verify'):
            revision.action_payslip_cancel()
        self.sudo().write({
            'state': 'refused',
            'refuse_reason': reason,
            'refused_by_id': self.env.uid,
        })
        self._post_step_note(
            '❌ Refused',
            _('Refused by %(user)s: %(reason)s',
              user=self.env.user.name, reason=reason),
        )
        self._notify_employee(
            _('Your payslip revision request %(ref)s was refused.',
              ref=self.name),
            reason,
        )

    def _open_reason_wizard(self, mode):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': (_('Refuse Revision Request') if mode == 'refuse'
                     else _('Return to HR')),
            'res_model': 'ksw.revision.request.reason.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_request_id': self.id,
                'default_mode': mode,
            },
        }

    # ==================================================================
    # Guards
    # ==================================================================

    def _check_step(self, expected_state, group, message):
        """State + authority in one place, called by every action method.

        View-level ``invisible=`` is cosmetic; any user with write access
        can call these over RPC (pitfall #15).
        """
        self.ensure_one()
        if self.state != expected_state:
            raise UserError(_(
                'This request is in state "%(state)s" and cannot be acted '
                'on at this step.',
                state=dict(self._STATES).get(self.state, self.state)))
        if self.env.su:
            return
        if not self.env.user.has_group(group):
            raise UserError(message)

    def _check_refusal_rights(self):
        """Whoever the request is currently waiting on may refuse it.

        One predicate for all three steps, called by the wizard as well as
        by the buttons, so the RPC route is guarded exactly like the UI one
        (pitfall #15).
        """
        self.ensure_one()
        if self.env.su:
            return
        if self.state == 'pending_hr':
            self._check_step('pending_hr', HR_GROUP, _(
                'Only the payroll team may refuse a request at the HR '
                'step.'))
        elif self.state == 'pending_gm':
            self._check_gm()
        elif self.state == 'pending_acc':
            self._check_step('pending_acc', ACC_GROUP, _(
                'Only the accounting team may refuse a request at the '
                'payment step.'))
        else:
            raise UserError(_(
                'A request in state "%(state)s" cannot be refused.',
                state=dict(self._STATES).get(self.state, self.state)))

    def _check_gm(self):
        """Raise unless the caller is this request's department GM."""
        self.ensure_one()
        if self.env.su:
            return
        gm_user = self._department_gm_user()
        if not gm_user:
            raise UserError(_(
                'No General Manager is set for %(dept)s, so this request '
                "cannot be approved. Ask HR to set the department's "
                'General Manager.',
                dept=(self.employee_id.sudo().department_id.display_name
                      or _('this employee')),
            ))
        if self.env.user != gm_user:
            raise UserError(_(
                'Only %(gm)s, the General Manager for %(dept)s, may approve '
                'this request.',
                gm=gm_user.name,
                dept=(self.employee_id.sudo().department_id.display_name
                      or _('employees with no department')),
            ))

    # ==================================================================
    # Chatter / notifications
    # ==================================================================

    def _post_step_note(self, title, detail):
        """Chatter note. sudo() because the acting user may hold no
        mail.message create right on this document (pitfall #11) — authority
        was established before we got here."""
        self.ensure_one()
        self.sudo().message_post(
            body=Markup(
                '<strong>%(title)s</strong><br/>%(detail)s'
            ) % {'title': title, 'detail': detail},
            subtype_xmlid='mail.mt_note',
        )

    def _notify_pending_approvers(self, pending_state):
        """Inbox + email to whoever must act on this step."""
        self.ensure_one()
        config = self._STEP_CONFIG.get(pending_state)
        if not config:
            return
        if config.get('department_gm'):
            gm_user = self._department_gm_user()
            partner_ids = (
                [gm_user.partner_id.id]
                if gm_user and gm_user.partner_id else [])
        else:
            group = self.env.ref(config['group'], raise_if_not_found=False)
            partner_ids = (
                group.sudo().user_ids.mapped('partner_id').ids
                if group else [])
        if not partner_ids:
            return
        self.sudo().message_post(
            body=Markup(
                '<strong>&#9203; Action Required — %(label)s</strong><br/>'
                '<b>Employee:</b> %(employee)s<br/>'
                '<b>Payslip:</b> %(slip)s<br/>'
                '<b>Period:</b> %(date_from)s &#8594; %(date_to)s<br/>'
                '<b>Complaint:</b> %(reason)s<br/>'
                '%(amount)s'
            ) % {
                'label': config['label'],
                'employee': self.employee_id.sudo().name,
                'slip': payslip_name(self.payslip_id.sudo()),
                'date_from': self.date_from,
                'date_to': self.date_to,
                'reason': self.reason or '',
                'amount': (
                    Markup('<b>Difference payable:</b> %(amt).2f SAR')
                    % {'amt': self.difference_amount}
                    if self.revision_payslip_id else Markup('')),
            },
            partner_ids=partner_ids,
            subtype_xmlid='mail.mt_comment',
        )

    def _employee_partner_ids(self):
        self.ensure_one()
        user = self.employee_id.sudo().user_id
        return [user.partner_id.id] if user and user.partner_id else []

    def _notify_employee(self, title, detail):
        self.ensure_one()
        partner_ids = self._employee_partner_ids()
        if not partner_ids:
            return
        self.sudo().message_post(
            body=Markup(
                '<strong>%(title)s</strong><br/>%(detail)s'
            ) % {'title': title, 'detail': detail},
            partner_ids=partner_ids,
            subtype_xmlid='mail.mt_comment',
        )

    def _notify_employee_paid(self):
        self.ensure_one()
        self._notify_employee(
            _('Your payslip revision request %(ref)s has been paid.',
              ref=self.name),
            _('%(amount).2f SAR was paid by %(method)s for the period '
              '%(date_from)s → %(date_to)s.',
              amount=self.difference_amount,
              method=dict(
                  self._fields['payment_method'].selection
              ).get(self.payment_method, self.payment_method or ''),
              date_from=self.date_from,
              date_to=self.date_to),
        )

    # ==================================================================
    # Navigation
    # ==================================================================

    def action_open_revision_payslip(self):
        self.ensure_one()
        if not self.revision_payslip_id:
            raise UserError(_('No revision payslip has been issued yet.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Revision Payslip'),
            'res_model': 'hr.payslip',
            'view_mode': 'form',
            'res_id': self.revision_payslip_id.id,
            'target': 'current',
        }

    def action_open_source_payslip(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Payslip'),
            'res_model': 'hr.payslip',
            'view_mode': 'form',
            'res_id': self.payslip_id.id,
            'target': 'current',
        }


def payslip_name(payslip):
    """A payslip's human reference, whatever it has been given."""
    if not payslip:
        return ''
    return payslip.number or payslip.name or str(payslip.id)
