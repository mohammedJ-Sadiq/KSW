from odoo import fields, models


class KswWorkshopRejectWizard(models.TransientModel):
    """Asks for the rejection reason at the moment of rejecting.

    The reason used to be a field on the request's Internal tab that had to be
    filled in before pressing Reject; the button only answered with an error
    naming a field the manager could not find.
    """
    _name = 'ksw.workshop.reject.wizard'
    _description = 'Reject Workshop Request'

    request_id = fields.Many2one('ksw.workshop.request', required=True, ondelete='cascade')
    rejection_reason = fields.Text(required=True)

    def action_confirm(self):
        self.ensure_one()
        # action_reject keeps every check (manager group, state, reason), so
        # the wizard adds no path around them.
        self.request_id.write({'rejection_reason': self.rejection_reason})
        self.request_id.action_reject()
        return {'type': 'ir.actions.act_window_close'}
