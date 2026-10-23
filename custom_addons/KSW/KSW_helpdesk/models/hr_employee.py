from odoo import api, models
from odoo.fields import Domain


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    @api.model
    def web_name_search(self, name, specification, domain=None, operator='ilike', limit=100):
        # "Reported By" dropdown on a helpdesk ticket (the field passes
        # ksw_helpdesk_reporter in its context). A plain employee's own search
        # is cut down by record rules (KSW_base_security only lets the
        # "Employee" tier see itself) and by the company rule, so managers
        # could not find their direct reports, nor anyone their own record
        # when it sits in another company. Search as sudo, but only inside
        # the reporter scope, and return names only.
        Ticket = self.env['helpdesk.ticket']
        if not self.env.context.get('ksw_helpdesk_reporter') or Ticket._is_helpdesk_agent():
            return super().web_name_search(name, specification, domain, operator, limit)
        scope = Domain(domain or []) & Domain('id', 'in', Ticket._reporter_employees().ids)
        pairs = self.sudo().name_search(name, scope, operator, limit)
        return [
            {'id': emp_id, 'display_name': emp_name, '__formatted_display_name': emp_name}
            for emp_id, emp_name in pairs
        ]
