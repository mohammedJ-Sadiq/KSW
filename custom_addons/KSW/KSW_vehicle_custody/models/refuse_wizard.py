from odoo import fields, models


class KswVehicleCustodyRefuseWizard(models.TransientModel):
    _name = 'ksw.vehicle.custody.refuse.wizard'
    _description = 'Refuse a Driver Change'

    request_id = fields.Many2one('ksw.vehicle.custody.request', required=True, readonly=True)
    reason = fields.Text(required=True)

    def action_confirm(self):
        self.request_id._refuse(self.reason)
        return {'type': 'ir.actions.act_window_close'}
