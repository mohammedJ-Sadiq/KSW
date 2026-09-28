import logging
import re

from markupsafe import Markup

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.fields import Domain

_logger = logging.getLogger(__name__)

# The three approver tiers, reused rather than reinvented (see the module
# README / brain note): the HR step is the payroll Officer tier that already
# owns "Issue Revision"; the GM step is routed to the requester's own
# department GM exactly as the annual-leave chain does; the accounting step
# is the existing Accounting Approver group.
HR_GROUP = 'om_hr_payroll.group_hr_payroll_user'
# The HR Approver of the annual-leave chain is HR for this document too: the
# people who review a salary complaint are the same people who review a leave,
# and they are not payroll operators — they hold no hr.payslip access and do
# not need any, because every payslip read on this document is sudo'd.  Naming
# the group here rather than making it imply the payroll Officer tier is the
# whole point: Officer is full CRUD on every payslip, batch and salary rule.
HR_LEAVE_GROUP = 'KSW_annual_leave.group_annual_leave_hr'
# Either group is "HR" at the review step.  Never test one of them alone.
HR_GROUPS = (HR_GROUP, HR_LEAVE_GROUP)
ACC_GROUP = 'KSW_annual_leave.group_annual_leave_acc'
GM_GROUP = 'KSW_annual_leave.group_annual_leave_gm'
# A difference paid in cash is handed over by the same cashier who hands out
# loan cash.  The group is declared in KSW_deduction, which depends on this
# module, so it is only ever resolved at runtime (``_is_disbursement_officer``)
# and its ACL, record rule and menus are declared over there.
DISB_GROUP = 'KSW_deduction.group_loan_disbursement'


class KswPayslipRevisionRequest(models.Model):
    """An employee's complaint that a confirmed payslip paid them short.

    Structurally a leave request: its own document, its own chatter, its own
    approval chain, its own notifications.  The payslip revision itself
    (``hr.payslip.x_is_revision``) is the *outcome* of the HR step, not the
    request — the same way an approved time-off request is what finally
    writes the attendance.

        draft → pending_dm → pending_hr → pending_gm → pending_acc
              → paid                                   (bank transfer)
              → pending_disbursement → paid            (cash)

    Refusal, with a reason, is available at every approval step up to and
    including accounting; the GM may also return the request to HR when the
    *figure* needs reworking rather than the complaint being wrong.  Once
    accounting has confirmed the revision payslip the money is committed,
    so the disbursement step can only confirm, never refuse.
    """

    _name = 'ksw.payslip.revision.request'
    _description = 'Payslip Revision Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    _STATES = [
        ('draft', 'Draft'),
        ('pending_dm', 'Pending DM Approval'),
        ('pending_hr', 'Pending HR Review'),
        ('pending_gm', 'Pending GM Approval'),
        ('pending_acc', 'Pending Accounting Payment'),
        ('pending_disbursement', 'Pending Disbursement'),
        ('paid', 'Paid'),
        ('refused', 'Refused'),
        ('cancelled', 'Cancelled'),
    ]

    # Which group is being waited on at each step, for notifications and for
    # the "Waiting for My Action" filter.  ``direct_manager`` and
    # ``department_gm`` route to one person rather than a whole group.
    _STEP_CONFIG = {
        'pending_dm': {'direct_manager': True,
                       'label': 'Direct Manager Approval'},
        'pending_hr': {'groups': HR_GROUPS, 'label': 'HR Review'},
        'pending_gm': {'department_gm': True, 'label': 'GM Approval'},
        'pending_acc': {'groups': (ACC_GROUP,), 'label': 'Accounting Payment'},
        'pending_disbursement': {'groups': (DISB_GROUP,),
                                 'label': 'Cash Disbursement'},
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
    dm_comment = fields.Text(string='DM Comment', tracking=True)
    dm_user_id = fields.Many2one('res.users', string='Approved By (DM)',
                                 readonly=True, copy=False)
    dm_date = fields.Datetime(string='Approved On (DM)', readonly=True,
                              copy=False)

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

    disbursed_by_id = fields.Many2one(
        'res.users', string='Disbursed By', readonly=True, copy=False)
    disbursed_date = fields.Datetime(
        string='Disbursed On', readonly=True, copy=False)

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
    can_dm_act = fields.Boolean(compute='_compute_permissions',
                                compute_sudo=False)
    can_hr_act = fields.Boolean(compute='_compute_permissions',
                                compute_sudo=False)
    can_gm_act = fields.Boolean(compute='_compute_permissions',
                                compute_sudo=False)
    can_acc_act = fields.Boolean(compute='_compute_permissions',
                                 compute_sudo=False)
    can_disburse = fields.Boolean(compute='_compute_permissions',
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
        is_hr = self._is_hr_reviewer(user)
        is_acc = user.has_group(ACC_GROUP)
        is_disb = self._is_disbursement_officer(user)
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
            req.can_cancel = req.state in (
                'draft', 'pending_dm', 'pending_hr') and (
                is_owner or is_manager or is_hr)
            req.can_dm_act = (
                req.state == 'pending_dm'
                and req._direct_manager_user() == user)
            req.can_hr_act = req.state == 'pending_hr' and is_hr
            req.can_gm_act = req.state == 'pending_gm' and is_gm
            req.can_acc_act = req.state == 'pending_acc' and is_acc
            req.can_disburse = req.state == 'pending_disbursement' and is_disb
            req.is_pending_my_action = (
                req.can_dm_act or req.can_hr_act or req.can_gm_act
                or req.can_acc_act or req.can_disburse)

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
        if self._is_hr_reviewer(user):
            states.append('pending_hr')
        if user.has_group(ACC_GROUP):
            states.append('pending_acc')
        if self._is_disbursement_officer(user):
            states.append('pending_disbursement')

        parts = []
        if states:
            parts.append(Domain('state', 'in', states))

        # The direct manager is resolved through sudo(): a manager holding
        # no HR rights cannot walk hr.employee.parent_id as themselves, and
        # this is an identity question, not a scope one.  Same predicate as
        # _direct_manager_user(): nobody is their own manager.
        reports = self.env['hr.employee'].sudo().with_context(
            active_test=False).search([
                ('parent_id.user_id', '=', user.id),
                ('user_id', '!=', user.id),
            ])
        if reports:
            parts.append(Domain('state', '=', 'pending_dm')
                         & Domain('employee_id', 'in', reports.ids))

        if user.has_group(GM_GROUP):
            parts.append(Domain('state', '=', 'pending_gm') & (
                Domain('employee_id.department_id.x_effective_gm_id.user_id',
                       '=', user.id)
                | (Domain('employee_id.department_id', '=', False)
                   & Domain('company_id.x_default_gm_id.user_id',
                            '=', user.id))))

        # Nothing can ever be pending this user's action.  `[]` would mean
        # "match everything" (pitfall #24); Domain.OR([]) is FALSE.
        domain = Domain.OR(parts)
        return domain if positive_wanted else ~domain

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

    def _direct_manager_user(self):
        """The user who must clear this request's DM step, or an empty
        recordset when there is none.

        ``employee_id.parent_id.user_id`` — the same "direct manager" this
        document already uses to decide who may file on an employee's
        behalf.  Read through sudo(): a plain employee may not read their
        manager's hr.employee row (pitfall #69).  An employee set as their
        own manager (the top of a hierarchy) has no one above them.
        """
        self.ensure_one()
        employee = (self.employee_id or self.payslip_id.employee_id).sudo()
        manager_user = employee.parent_id.user_id
        if not manager_user or manager_user == employee.user_id:
            return self.env['res.users']
        return manager_user

    @api.model
    def _is_disbursement_officer(self, user=None):
        """Does this user hand out cash for a revision paid in cash?

        The group lives in KSW_deduction, which depends on this module, so
        it may be absent from the registry (KSW_payroll's own tests run
        before KSW_deduction is loaded) — has_group() on a missing xmlid
        raises, hence the lookup first.
        """
        user = user or self.env.user
        if not self.env.ref(DISB_GROUP, raise_if_not_found=False):
            return False
        return user.has_group(DISB_GROUP)

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
        if self._is_hr_reviewer(user):
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
    _DM_WRITABLE = {'dm_comment'}
    _HR_WRITABLE = {'hr_comment', 'attachment_ids'}
    _GM_WRITABLE = {'gm_comment'}
    _ACC_WRITABLE = {'payment_method', 'payment_reference'}
    # The cashier records the receipt they handed over, nothing else — the
    # method is accounting's decision and is already fixed.
    _DISB_WRITABLE = {'payment_reference'}

    def _allowed_write_fields(self):
        """The fields the current user may write on this record right now.

        A whitelist, not a state check: write access on the document would
        otherwise let the employee set ``state`` to ``paid`` over RPC and
        skip all three approvers (pitfall #15).
        """
        self.ensure_one()
        employee = self.employee_id.sudo()
        user = self.env.user
        is_hr = self._is_hr_reviewer(user)
        is_filer = (
            employee.user_id == user
            or employee.parent_id.user_id == user
            or is_hr
        )
        if self.state == 'draft' and is_filer:
            return set(self._DRAFT_WRITABLE)
        if self.state == 'pending_dm' and self._direct_manager_user() == user:
            return set(self._DM_WRITABLE)
        if self.state == 'pending_hr' and is_hr:
            return set(self._HR_WRITABLE)
        if self.state == 'pending_gm' and self._is_department_gm(user):
            return set(self._GM_WRITABLE)
        if self.state == 'pending_acc' and user.has_group(ACC_GROUP):
            return set(self._ACC_WRITABLE)
        if (self.state == 'pending_disbursement'
                and self._is_disbursement_officer(user)):
            return set(self._DISB_WRITABLE)
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
            manager_user = req._direct_manager_user()
            if manager_user and manager_user != self.env.user:
                req.sudo().write({'state': 'pending_dm'})
                req._post_step_note(
                    '📨 Submitted',
                    _('Sent to the direct manager, %(dm)s, for approval.',
                      dm=manager_user.name))
                req._notify_pending_approvers('pending_dm')
                continue
            # No DM step to wait on: the manager filed it themselves (their
            # submission *is* the approval, stamped as such), or the
            # employee has no manager with a user — waiting on nobody would
            # stall the request forever.
            vals = {'state': 'pending_hr'}
            if manager_user:
                vals.update({
                    'dm_user_id': manager_user.id,
                    'dm_date': fields.Datetime.now(),
                })
                detail = _('Filed by the direct manager, so the DM step is '
                           'approved with the submission. Sent to HR for '
                           'review.')
            else:
                detail = _('The employee has no direct manager with a user '
                           'account, so the DM step is skipped. Sent to HR '
                           'for review.')
            req.sudo().write(vals)
            req._post_step_note('📨 Submitted', detail)
            req._notify_pending_approvers('pending_hr')
        return True

    # ==================================================================
    # Step 1 — the direct manager approves the complaint going forward
    # ==================================================================

    def action_dm_approve(self):
        for req in self:
            req._check_dm()
            req.sudo().write({
                'state': 'pending_hr',
                'dm_user_id': self.env.uid,
                'dm_date': fields.Datetime.now(),
            })
            detail = _('Approved by %(user)s. Sent to HR for review.',
                       user=self.env.user.name)
            if (req.dm_comment or '').strip():
                detail = Markup('%(detail)s<br/><b>Comment:</b> %(note)s') % {
                    'detail': detail, 'note': req.dm_comment}
            req._post_step_note('✅ Approved by Direct Manager', detail)
            req._notify_pending_approvers('pending_hr')
        return True

    def action_dm_refuse(self):
        self.ensure_one()
        self._check_dm()
        return self._open_reason_wizard('refuse')

    # ==================================================================
    # Step 2 — HR checks the complaint and issues the revision
    # ==================================================================

    def action_hr_accept(self):
        """HR agrees with the complaint: issue the revision payslip.

        The revision is left in **draft** so the GM approves a figure that
        was actually computed rather than a promise, and so accounting can
        still recompute it right before paying — confirming a payslip is a
        recompute, so a file exported from a draft can stop matching.
        """
        for req in self:
            req._check_step('pending_hr', HR_GROUPS, _(
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
        self._check_step('pending_hr', HR_GROUPS, _(
            'Only the payroll team may review a revision request.'))
        return self._open_reason_wizard('refuse')

    # ==================================================================
    # Step 3 — the GM approves the figure
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
    # Step 4 — accounting confirms the revision and pays
    # ==================================================================

    def action_acc_pay(self):
        """Confirm the revision payslip, then pay by the chosen method.

        Confirming is deliberately here and not earlier: ``action_payslip_
        done`` recomputes the sheet, so the figure is fixed at the moment
        the money is committed and the bank file is generated from a
        payslip that can no longer move.

        * **Bank transfer** — paid now, and both bank files (Excel + TXT)
          are produced on the spot and filed on the request.  If either
          cannot be produced the whole step rolls back: a bank payment with
          no file to send the bank is not a payment.
        * **Cash** — the money has not left yet, so the request waits in
          ``pending_disbursement`` for the Loan Disbursement Officer to
          confirm they handed it over, exactly as a loan does.
        """
        for req in self:
            req._check_step('pending_acc', (ACC_GROUP,), _(
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
            stamps = {
                'acc_user_id': self.env.uid,
                'acc_date': fields.Datetime.now(),
            }
            if req.payment_method == 'cash':
                req.sudo().write(dict(stamps, state='pending_disbursement'))
                req._post_step_note(
                    '💵 Payment confirmed — awaiting cash disbursement',
                    _('Revision %(slip)s confirmed; %(amount).2f SAR to be '
                      'handed over in cash.',
                      slip=payslip_name(revision),
                      amount=req.difference_amount),
                )
                req._notify_pending_approvers('pending_disbursement')
                req._notify_employee(
                    _('Your payslip revision request %(ref)s is ready for '
                      'cash collection.', ref=req.name),
                    _('%(amount).2f SAR has been approved. Please collect it '
                      'from the disbursement officer; the request is closed '
                      'once the handover is confirmed.',
                      amount=req.difference_amount),
                )
                continue
            req.sudo().write(dict(
                stamps, state='paid',
                payment_date=fields.Date.context_today(req)))
            req._post_step_note(
                '💵 Paid',
                _('%(amount).2f SAR paid by %(method)s.',
                  amount=req.difference_amount,
                  method=req._payment_method_label()),
            )
            # Both files, now: the bank transfer is only real once there is
            # a file to hand the bank.  Each is re-homed onto the request
            # under Supporting Documents by _file_export_on_request().
            try:
                req._generate_bank_file('all_excel')
                req._generate_bank_file('all_txt')
            except UserError as exc:
                # Raising rolls the whole step back — revision confirmation
                # included — so say plainly that nothing was paid, and
                # replace the exporter's batch-level wording ("no fallback
                # on the batch") with what accounting can actually fix.
                raise UserError(_(
                    'Payment NOT recorded — the bank file for %(employee)s '
                    'could not be produced, so nothing was confirmed or '
                    'paid.\n\nReason: %(reason)s\n\nFix: set the '
                    "employee's Salary Paying Bank Account (and its Payroll "
                    'File Type) and press Confirm & Pay again, or choose '
                    'Cash as the payment method.',
                    employee=req.employee_id.sudo().name,
                    reason=exc.args[0] if exc.args else str(exc),
                )) from exc
            req._notify_employee_paid()
        return True

    # ==================================================================
    # Step 5 (cash only) — the cash is handed over
    # ==================================================================

    def action_disbursement_confirm(self):
        """The disbursement officer confirms the cash left the till.

        The loan's Step 5, for the same reason: accounting's approval says
        the money *may* be paid, only the person who hands it over can say
        it *was*.  There is no refusal here — the revision payslip is
        already confirmed, so the money is owed either way.
        """
        for req in self:
            if req.state != 'pending_disbursement':
                raise UserError(_(
                    'This request is not waiting for cash disbursement.'))
            if not self.env.su and not self._is_disbursement_officer():
                raise UserError(_(
                    'Only the Loan Disbursement Officer can confirm a cash '
                    'disbursement.'))
            req.sudo().write({
                'state': 'paid',
                'disbursed_by_id': self.env.uid,
                'disbursed_date': fields.Datetime.now(),
                'payment_date': fields.Date.context_today(req),
            })
            req._post_step_note(
                '✅ Cash disbursed',
                _('%(amount).2f SAR handed over by %(user)s.',
                  amount=req.difference_amount, user=self.env.user.name),
            )
            req._notify_employee_paid()
        return True

    def _payment_method_label(self):
        self.ensure_one()
        return dict(self._fields['payment_method'].selection).get(
            self.payment_method, self.payment_method or '')

    def action_acc_refuse(self):
        self.ensure_one()
        self._check_step('pending_acc', (ACC_GROUP,), _(
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
                or self._is_hr_reviewer()):
            raise UserError(_(
                'Only the accounting or payroll team may generate the bank '
                'file for a revision.'))
        if self.state not in ('pending_acc', 'paid'):
            raise UserError(_(
                'The bank file can only be generated once the GM has '
                'approved this request.'))
        if self.payment_method != 'bank':
            raise UserError(_(
                'A bank file is only produced for a bank transfer. This '
                'difference is being paid in cash.'))
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
                if not (self._is_hr_reviewer()
                        or employee.user_id == self.env.user
                        or employee.parent_id.user_id == self.env.user):
                    raise UserError(_(
                        'You may only reopen your own request.'))
            # Another complaint may have been filed and accepted in the
            # meantime; reopening must not produce two open requests for
            # the same payslip.
            if not self.env.su:
                req._check_can_file(req.payslip_id.sudo(), exclude=req)
            # The DM approved the complaint as it stood; a reopened request
            # can be edited, so it needs their approval again.
            req.sudo().write({
                'state': 'draft',
                'refuse_reason': False,
                'refused_by_id': False,
                'dm_user_id': False,
                'dm_date': False,
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

    def _check_step(self, expected_state, groups, message):
        """State + authority in one place, called by every action method.

        View-level ``invisible=`` is cosmetic; any user with write access
        can call these over RPC (pitfall #15).  ``groups`` is a tuple and
        holding *any one* of them is enough: a step may be owned by more
        than one role — the HR step is held by the payroll Officer and by
        the annual-leave HR Approver alike.
        """
        self.ensure_one()
        if isinstance(groups, str):
            groups = (groups,)
        if self.state != expected_state:
            raise UserError(_(
                'This request is in state "%(state)s" and cannot be acted '
                'on at this step.',
                state=dict(self._STATES).get(self.state, self.state)))
        if self.env.su:
            return
        if not any(self.env.user.has_group(g) for g in groups):
            raise UserError(message)

    @api.model
    def _is_hr_reviewer(self, user=None):
        """Is this user HR for the review step?

        One predicate, called from every guard and every gate field, so the
        two HR roles can never drift apart — a role that may press the
        button but is missing from the "Waiting for My Action" filter is
        exactly the shape this gap took.
        """
        user = user or self.env.user
        return any(user.has_group(g) for g in HR_GROUPS)

    def _check_refusal_rights(self):
        """Whoever the request is currently waiting on may refuse it.

        One predicate for all three steps, called by the wizard as well as
        by the buttons, so the RPC route is guarded exactly like the UI one
        (pitfall #15).
        """
        self.ensure_one()
        if self.env.su:
            return
        if self.state == 'pending_dm':
            self._check_dm()
        elif self.state == 'pending_hr':
            self._check_step('pending_hr', HR_GROUPS, _(
                'Only the payroll team may refuse a request at the HR '
                'step.'))
        elif self.state == 'pending_gm':
            self._check_gm()
        elif self.state == 'pending_acc':
            self._check_step('pending_acc', (ACC_GROUP,), _(
                'Only the accounting team may refuse a request at the '
                'payment step.'))
        else:
            raise UserError(_(
                'A request in state "%(state)s" cannot be refused.',
                state=dict(self._STATES).get(self.state, self.state)))

    def _check_dm(self):
        """Raise unless the request is at the DM step and the caller is
        the employee's direct manager."""
        self.ensure_one()
        if self.state != 'pending_dm':
            raise UserError(_(
                'This request is not waiting for the direct manager.'))
        if self.env.su:
            return
        manager_user = self._direct_manager_user()
        if self.env.user != manager_user:
            raise UserError(_(
                'Only %(dm)s, the direct manager of %(employee)s, may approve '
                'or refuse this request at this step.',
                dm=manager_user.name or _('the direct manager'),
                employee=self.employee_id.sudo().name))

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
        if config.get('direct_manager'):
            manager_user = self._direct_manager_user()
            partner_ids = (
                [manager_user.partner_id.id]
                if manager_user and manager_user.partner_id else [])
        elif config.get('department_gm'):
            gm_user = self._department_gm_user()
            partner_ids = (
                [gm_user.partner_id.id]
                if gm_user and gm_user.partner_id else [])
        else:
            partners = self.env['res.partner']
            for xmlid in config.get('groups', ()):
                group = self.env.ref(xmlid, raise_if_not_found=False)
                if group:
                    partners |= group.sudo().user_ids.partner_id
            partner_ids = partners.ids
        if not partner_ids:
            _logger.warning(
                'Revision request %s reached %s with nobody to notify.',
                self.name, pending_state)
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
              method=self._payment_method_label(),
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
