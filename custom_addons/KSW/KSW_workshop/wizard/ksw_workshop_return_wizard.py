from odoo import fields, models


class KswWorkshopReturnWizard(models.TransientModel):
    """Asks for the reason at the moment the manager returns a request.

    Replaced the Reject prompt (Oct 2026): the manager no longer rejects a
    request, he sends it back to the requester to correct and resubmit.
    """
    _name = 'ksw.workshop.return.wizard'
    _description = 'Return Workshop Request to Requester'

    request_id = fields.Many2one('ksw.workshop.request', required=True, ondelete='cascade')
    return_reason = fields.Text(required=True)

    def action_confirm(self):
        self.ensure_one()
        # action_return keeps every check (manager group, state, reason), so
        # the wizard adds no path around them.
        self.request_id.action_return(self.return_reason)
        return {'type': 'ir.actions.act_window_close'}
