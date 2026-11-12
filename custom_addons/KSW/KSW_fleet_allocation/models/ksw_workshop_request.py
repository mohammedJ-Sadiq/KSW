from odoo import _, api, fields, models
from odoo.exceptions import UserError


class KswWorkshopRequest(models.Model):
    _inherit = 'ksw.workshop.request'

    x_handover_ids = fields.One2many('ksw.vehicle.handover', 'workshop_request_id', string='Handover Forms')
    # sudo count: the button shows it to workshop staff, who may not read
    # every form, and a failed o2m read would take the whole form down.
    x_handover_count = fields.Integer(compute='_compute_handover_count', compute_sudo=True)
    x_handover_pending = fields.Boolean(compute='_compute_handover_count', compute_sudo=True)

    @api.depends('x_handover_ids.state')
    def _compute_handover_count(self):
        for request in self:
            forms = request.x_handover_ids.filtered(lambda f: f.state != 'cancelled')
            request.x_handover_count = len(forms)
            request.x_handover_pending = bool(forms.filtered(lambda f: f.state == 'draft'))

    def action_complete(self):
        # A checkup raised by the GM's holding decision closes only with its
        # HR-12 signed and filed: completing it is what lets the vehicle out
        # of the Holding pool.
        pending = self.sudo().x_handover_ids.filtered(lambda f: f.state == 'draft')
        if pending:
            raise UserError(_(
                'File the signed handover form first (%s): fill it, print it, have it signed, '
                'attach the scan and press Mark as Signed.', ', '.join(pending.mapped('name'))))
        return super().action_complete()

    def action_open_handover_forms(self):
        self.ensure_one()
        forms = self.sudo().x_handover_ids.filtered(lambda f: f.state != 'cancelled')
        action = self.env['ir.actions.act_window']._for_xml_id('KSW_fleet_allocation.action_ksw_vehicle_handover')
        if len(forms) == 1:
            action.update({'res_id': forms.id, 'view_mode': 'form', 'views': [(False, 'form')]})
        else:
            action['domain'] = [('workshop_request_id', '=', self.id)]
        return action
