from odoo import fields, models


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    # The clients this driver actually serves. Optional, and it NARROWS the
    # branch list rather than replacing it: in BAS, 41 drivers served one client
    # over three months and 43 served two, but the tail runs past twenty, and it
    # changes week to week. A per-driver list is worth having and is the wrong
    # thing to depend on.
    #
    # No model-level `groups=` on purpose. KSW gotcha #32 is about *stored*
    # columns on hr.employee breaking a non-HR user's own record fetch through
    # hr.employee.public; a Many2many is not a column on that table, so it is
    # not in the fetch set. Who may EDIT it is a view-level question, and the
    # editors are dispatchers, not HR.
    x_water_client_ids = fields.Many2many(
        'res.partner', 'ksw_water_driver_client_rel', 'employee_id', 'partner_id',
        string='Water Clients',
        domain="[('customer_rank', '>', 0)]",
        help='Leave empty to let this driver deliver to every client his '
             'branch serves. Fill it in to narrow him to these clients only.',
    )
