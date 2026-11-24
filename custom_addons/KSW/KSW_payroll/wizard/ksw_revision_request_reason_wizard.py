from odoo import api, fields, models, _
from odoo.exceptions import UserError


class KswRevisionRequestReasonWizard(models.TransientModel):
    """Collects the mandatory reason for refusing — or returning — a
    payslip revision request.

    One wizard for both verbs: the two differ only in where the request
    lands afterwards, and a shared dialog means the reason is mandatory on
    both paths rather than on whichever one someone remembered.
    """

    _name = 'ksw.revision.request.reason.wizard'
    _description = 'Payslip Revision Request — Reason'

    # Listed in the form arch as an invisible field.  A default reaching
    # default_get is NOT the same as reaching the client's record: the
    # onchange that feeds the computes below omits any field the view did
    # not ask for (pitfall #40).
    request_id = fields.Many2one(
        'ksw.payslip.revision.request', string='Request',
        required=True, readonly=True,
    )
    mode = fields.Selection(
        [('refuse', 'Refuse'), ('return', 'Return')],
        string='Action', required=True, readonly=True, default='refuse',
    )
    # A static Selection is enough here, unlike the leave and loan return
    # wizards: the options differ only by the request's state, and the radio
    # is shown only at the accounting step, where both are valid. From the
    # GM step the one target, HR, is the default (pitfall #39).
    return_target = fields.Selection(
        [('pending_hr', 'HR Review'), ('pending_gm', 'GM Approval')],
        string='Return To', default='pending_hr',
    )
    request_state = fields.Selection(
        related='request_id.state', string='Request Status')
    reason = fields.Text(string='Reason', required=True)

    employee_id = fields.Many2one(
        related='request_id.employee_id', string='Employee', readonly=True)
    difference_amount = fields.Float(
        related='request_id.difference_amount', string='Difference Payable',
        readonly=True)

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        if not vals.get('request_id') and self.env.context.get(
                'active_model') == 'ksw.payslip.revision.request':
            vals['request_id'] = self.env.context.get('active_id')
        return vals

    def action_confirm(self):
        self.ensure_one()
        reason = (self.reason or '').strip()
        if not reason:
            raise UserError(_('A reason is required.'))
        request = self.request_id
        if self.mode == 'return':
            # Authority is re-checked on the request itself: the wizard is
            # reachable over RPC like any other model.
            target = self.return_target or 'pending_hr'
            request._check_return_rights(target)
            request._apply_return(target, reason)
        else:
            request._check_refusal_rights()
            request._apply_refusal(reason)
        return {'type': 'ir.actions.act_window_close'}
