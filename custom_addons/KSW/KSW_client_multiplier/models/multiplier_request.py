"""A request to change a client's «الرد المضاعف».

Draft → Waiting for GM → Approved / Refused (or Cancelled by the requester).

* **Who asks**: a Water Delivery Supervisor (``group_water_dispatcher``),
  for any client, with a reason and the date the new value starts.
* **Who decides**: the requester's department GM — the same predicate the
  time-off and loan chains use, ``hr.department.x_effective_gm_id`` (which
  already climbs to the parent department), else the company default GM.
  Authority is the person named on the record, never a group: a group cannot
  say *for whom* (KSW, Aug 2026). Resolved at submission and stored, because
  the record rule that lets the GM see the request needs a column.
* **What approval does**: adds one dated line to the client's history. Loads
  before the effective date keep the old value; nothing is rewritten.

After submission nothing on the request is editable; every state change goes
through an action that checks the caller first and then writes as sudo.
"""
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

SUPERVISOR = 'KSW_water_delivery.group_water_dispatcher'


class KswClientMultiplierRequest(models.Model):
    _name = 'ksw.client.multiplier.request'
    _description = 'Client Multiplier Change Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(string='Reference', readonly=True, copy=False, default='/')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('pending', 'Waiting for GM'),
        ('approved', 'Approved'),
        ('refused', 'Refused'),
        ('cancel', 'Cancelled'),
    ], string='Status', default='draft', required=True, readonly=True, tracking=True, index=True)

    client_id = fields.Many2one('ksw.client.multiplier', string='Client', required=True,
                                index=True, tracking=True)
    bas_code = fields.Char(related='client_id.bas_code', string='Account Code')
    current_multiplier = fields.Float(
        related='client_id.current_multiplier', string='Multiplier Today')
    old_multiplier = fields.Float(
        string='Multiplier Before', readonly=True, digits=(16, 2),
        help='The value in force on the effective date when the request was submitted.')
    new_multiplier = fields.Float(string='New Multiplier', required=True, digits=(16, 2),
                                  tracking=True)
    effective_date = fields.Date(string='Effective From', required=True, tracking=True,
                                 default=fields.Date.context_today)
    reason = fields.Text(string='Reason', required=True)

    requested_by_id = fields.Many2one('res.users', string='Requested By', readonly=True,
                                      default=lambda self: self.env.user)
    request_date = fields.Date(string='Request Date', readonly=True,
                               default=fields.Date.context_today)
    department_id = fields.Many2one('hr.department', string='Department', readonly=True)
    company_id = fields.Many2one('res.company', readonly=True,
                                 default=lambda self: self.env.company)
    gm_employee_id = fields.Many2one(
        'hr.employee', string='Approver (GM)', compute='_compute_gm', store=True)
    gm_user_id = fields.Many2one(
        'res.users', string='GM User', compute='_compute_gm', store=True, index=True)

    approved_by_id = fields.Many2one('res.users', string='Decided By', readonly=True)
    approved_on = fields.Datetime(string='Decided On', readonly=True)
    refuse_reason = fields.Text(string='Refusal Reason', readonly=True)

    can_approve = fields.Boolean(compute='_compute_permissions')
    can_cancel = fields.Boolean(compute='_compute_permissions')

    # ------------------------------------------------------------------
    @api.depends('department_id.x_effective_gm_id.user_id',
                 'company_id.x_default_gm_id.user_id', 'state')
    def _compute_gm(self):
        for rec in self:
            if rec.state == 'draft':
                # Unknown until it is sent: the requester's department is
                # stamped at submission.
                rec.gm_employee_id = rec.gm_user_id = False
                continue
            gm = rec.department_id.sudo().x_effective_gm_id \
                or rec.company_id.sudo().x_default_gm_id
            rec.gm_employee_id = gm
            rec.gm_user_id = gm.sudo().user_id

    @api.depends_context('uid')
    @api.depends('state', 'gm_user_id', 'requested_by_id')
    def _compute_permissions(self):
        user = self.env.user
        for rec in self:
            rec.can_approve = rec.state == 'pending' and rec.gm_user_id == user
            rec.can_cancel = rec.state in ('draft', 'pending') and (
                rec.requested_by_id == user or user.has_group('base.group_system'))

    @api.constrains('new_multiplier')
    def _check_multiplier(self):
        for rec in self:
            if rec.new_multiplier <= 0:
                raise ValidationError(_('The new multiplier must be greater than zero.'))

    # ------------------------------------------------------------------
    # Nothing changes after submission except through the actions below.
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and not self.env.user.has_group(SUPERVISOR):
            raise UserError(_('Only a Water Delivery Supervisor can request a multiplier change.'))
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su:
            locked = self.filtered(lambda r: r.state != 'draft')
            if locked:
                raise UserError(_('%(ref)s has been sent and can no longer be edited.',
                                  ref=locked[0].name))
            if not self.env.user.has_group(SUPERVISOR):
                raise UserError(_('Only a Water Delivery Supervisor can edit a request.'))
        return super().write(vals)

    def unlink(self):
        if not self.env.su and self.filtered(lambda r: r.state != 'draft'):
            raise UserError(_('A request that has been sent is kept; cancel it instead.'))
        return super().unlink()

    # ------------------------------------------------------------------
    def action_submit(self):
        if not self.env.su and not self.env.user.has_group(SUPERVISOR):
            raise UserError(_('Only a Water Delivery Supervisor can send a request.'))
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_('%(ref)s has already been sent.', ref=rec.name))
            if rec.effective_date < rec.request_date:
                raise UserError(_(
                    'The effective date cannot be before the day the change was '
                    'requested (%(day)s): a change applies from now on, never to '
                    'loads already delivered.', day=rec.request_date))
            other = self.sudo().search([('client_id', '=', rec.client_id.id),
                                        ('state', '=', 'pending'), ('id', '!=', rec.id)], limit=1)
            if other:
                raise UserError(_('%(client)s already has a request waiting for the GM (%(ref)s).',
                                  client=rec.client_id.name, ref=other.name))
            before = rec.client_id.sudo()._rate_on(rec.effective_date)
            if before and abs(before.multiplier - rec.new_multiplier) < 0.0001:
                raise UserError(_('%(client)s is already at %(value)s on that date.',
                                  client=rec.client_id.name, value=rec.new_multiplier))
            employee = rec.requested_by_id.sudo().employee_id
            rec.sudo().write({
                'state': 'pending',
                'name': self.env['ir.sequence'].sudo().next_by_code(
                    'ksw.client.multiplier.request') or '/',
                'old_multiplier': before.multiplier if before else 0.0,
                'department_id': employee.department_id.id,
                'company_id': (employee.company_id or rec.company_id).id,
            })
            if not rec.gm_user_id:
                raise UserError(_(
                    'No General Manager is set for %(dept)s, so nobody could approve '
                    'this request. Ask HR to set the department\'s General Manager '
                    '(or the company default GM).',
                    dept=rec.department_id.display_name or _('your department')))
            rec.sudo().activity_schedule(
                'mail.mail_activity_data_todo', user_id=rec.gm_user_id.id,
                summary=_('Approve «الرد المضاعف» change for %(client)s', client=rec.client_id.name))
            rec.sudo().message_post(
                body=Markup(_('Sent to %(gm)s for approval: %(old)s → <b>%(new)s</b> from '
                              '%(day)s.<br/>Reason: %(reason)s')) % {
                    'gm': rec.gm_user_id.name, 'old': rec.old_multiplier,
                    'new': rec.new_multiplier, 'day': rec.effective_date, 'reason': rec.reason},
                subtype_xmlid='mail.mt_note')
        return True

    def _check_gm(self):
        if self.env.su:
            return
        for rec in self:
            if rec.state != 'pending':
                raise UserError(_('%(ref)s is not waiting for approval.', ref=rec.name))
            if rec.gm_user_id != self.env.user:
                raise UserError(_('Only %(gm)s, the General Manager of %(dept)s, can decide on '
                                  '%(ref)s.', gm=rec.gm_user_id.name or _('the GM'),
                                  dept=rec.department_id.display_name or _('this department'),
                                  ref=rec.name))

    def action_approve(self):
        self._check_gm()
        for rec in self:
            if rec.state != 'pending':
                raise UserError(_('%(ref)s is not waiting for approval.', ref=rec.name))
            rec.sudo()._apply()
            rec.sudo().write({'state': 'approved', 'approved_by_id': self.env.user.id,
                              'approved_on': fields.Datetime.now()})
            rec._close_activities(_('Approved'))
            rec.sudo().message_post(
                body=Markup(_('Approved. %(client)s is at <b>%(new)s</b> from %(day)s; loads '
                              'before that keep %(old)s.')) % {
                    'client': rec.client_id.name, 'new': rec.new_multiplier,
                    'day': rec.effective_date, 'old': rec.old_multiplier},
                partner_ids=rec.requested_by_id.partner_id.ids,
                subtype_xmlid='mail.mt_comment')
            rec.client_id.sudo().message_post(
                body=Markup(_('«الرد المضاعف» %(old)s → <b>%(new)s</b> from %(day)s (%(ref)s, '
                              'approved by %(gm)s).')) % {
                    'old': rec.old_multiplier, 'new': rec.new_multiplier,
                    'day': rec.effective_date, 'ref': rec.name, 'gm': self.env.user.name},
                subtype_xmlid='mail.mt_note')
        return True

    def _apply(self):
        """Add the approved value to the client's history, from its effective date."""
        Rate = self.env['ksw.client.multiplier.rate'].sudo()
        for rec in self:
            same_day = Rate.search([('client_id', '=', rec.client_id.id),
                                    ('date_from', '=', rec.effective_date)])
            vals = {'multiplier': rec.new_multiplier, 'source': 'request', 'request_id': rec.id}
            if same_day:
                same_day.write(vals)
            else:
                Rate.create(dict(vals, client_id=rec.client_id.id, date_from=rec.effective_date))

    def action_refuse_open(self):
        self._check_gm()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Refuse'),
            'res_model': 'ksw.client.multiplier.refuse.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_request_id': self.id},
        }

    def _refuse(self, reason):
        self._check_gm()
        for rec in self:
            rec.sudo().write({'state': 'refused', 'refuse_reason': reason,
                              'approved_by_id': self.env.user.id,
                              'approved_on': fields.Datetime.now()})
            rec._close_activities(_('Refused'))
            rec.sudo().message_post(
                body=Markup(_('Refused by %(gm)s.<br/>Reason: %(reason)s')) % {
                    'gm': self.env.user.name, 'reason': reason},
                partner_ids=rec.requested_by_id.partner_id.ids,
                subtype_xmlid='mail.mt_comment')

    def action_cancel(self):
        for rec in self:
            rec._compute_permissions()
            if not self.env.su and not rec.can_cancel:
                raise UserError(_('Only the requester can cancel %(ref)s, and only before '
                                  'the GM decides.', ref=rec.name))
            rec.sudo().write({'state': 'cancel'})
            rec._close_activities(_('Cancelled'))
        return True

    def _close_activities(self, feedback):
        self.sudo().activity_ids.action_feedback(feedback=feedback)

    def _open(self):
        return {'type': 'ir.actions.act_window', 'res_model': self._name,
                'res_id': self.id, 'view_mode': 'form', 'target': 'current'}
