from odoo import fields, models


class KswClientMultiplierRefuseWizard(models.TransientModel):
    _name = 'ksw.client.multiplier.refuse.wizard'
    _description = 'Refuse a Multiplier Change'

    request_id = fields.Many2one('ksw.client.multiplier.request', required=True)
    reason = fields.Text(string='Reason', required=True)

    def action_confirm(self):
        self.request_id._refuse(self.reason)
        return {'type': 'ir.actions.act_window_close'}
