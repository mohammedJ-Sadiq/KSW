from odoo import api, fields, models


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    # The inverse of ksw.workshop.technician.employee_id, and the same shape
    # res_partner.py uses for the client role: a One2many adds no column, so
    # none of the stored-field traps on hr.employee apply (a bare stored field
    # here breaks a regular employee's own Time Off form — Odoo 19 Pitfalls #32).
    x_workshop_technician_ids = fields.One2many(
        'ksw.workshop.technician', 'employee_id', string='Workshop Technician Registration')

    # Deliberately NOT a stored Boolean. The register is the single source of
    # truth; this is the checkbox onto it, so there is nothing to keep in step.
    x_is_workshop_technician = fields.Boolean(
        string='Workshop Technician',
        compute='_compute_x_is_workshop_technician',
        inverse='_inverse_x_is_workshop_technician',
        search='_search_x_is_workshop_technician',
        help="Lists this employee in the Technician column of a workshop repair's "
             "Labor / Service table.")

    @api.depends('x_workshop_technician_ids')
    def _compute_x_is_workshop_technician(self):
        for employee in self:
            employee.x_is_workshop_technician = bool(employee.x_workshop_technician_ids)

    def _inverse_x_is_workshop_technician(self):
        # sudo(): the register is workshop configuration, but the act of
        # flagging is an HR one on the employee form. Whoever got this far
        # already has write access on the employee record, which is the
        # authority being checked.
        Technician = self.env['ksw.workshop.technician'].sudo()
        for employee in self:
            # active_test=False, then revive: unregistering archives the row
            # (so past labor lines keep naming someone), and a plain create on
            # the second tick would hit the unique index instead. Same
            # arrangement as ksw.workshop.client.create().
            existing = Technician.with_context(active_test=False).search(
                [('employee_id', '=', employee.id)], limit=1)
            if employee.x_is_workshop_technician:
                if existing:
                    existing.active = True
                else:
                    Technician.create({'employee_id': employee.id})
            elif existing:
                existing.active = False

    def _search_x_is_workshop_technician(self, operator, value):
        # Odoo 19 rewrites '='/'!=' on a boolean into 'in'/'not in' with an
        # OrderedSet value before this is ever called, so branch on the
        # operator and never on the value's type (Odoo 19 Pitfalls #24).
        if operator in ('in', 'not in'):
            positive_wanted = (operator == 'in') == any(value)
        elif operator in ('=', '!='):
            positive_wanted = (operator == '=') == bool(value)
        else:
            return NotImplemented
        registered = self.env['ksw.workshop.technician'].sudo().search(
            [('employee_id', '!=', False)]).employee_id.ids
        return [('id', 'in' if positive_wanted else 'not in', registered)]
