from odoo import models


class KswFleetVehicle(models.Model):
    _inherit = 'ksw.fleet.vehicle'

    def _ksw_set_custodian(self, employee, source=False, date=False, note=False):
        """A transfer waiting only for its employee's vehicles applies as
        soon as the last one is handed over, not at the next nightly run."""
        previous = self.sudo().x_custodian_id
        res = super()._ksw_set_custodian(employee, source=source, date=date, note=note)
        if previous:
            self.env['ksw.employee.transfer'].sudo().search([
                ('state', '=', 'approved'), ('employee_id', 'in', previous.ids)])._apply_due()
        return res
