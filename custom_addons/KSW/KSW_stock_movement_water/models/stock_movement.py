from odoo import fields, models


class KswStockMovementLine(models.TransientModel):
    _inherit = 'ksw.stock.movement.line'

    # Names, not relations: the rows are a snapshot, and a stock user has no
    # read on hr.employee (a relation to it would fail on his screen).
    x_dispatcher = fields.Char(
        string='Dispatcher', readonly=True,
        help='Who dispatched or sold it at the desk (water delivery notes made '
             'from a dispatch). Empty when the driver issued it himself.')
    # The delivery's cost centres, invoiced or not: an invoice is what puts a
    # sale into the analytic reports, so this is where a delivery made but
    # not billed yet is seen by cost centre.
    x_cc_branch_id = fields.Many2one('account.analytic.account', string='Branch Cost Centre',
                                     readonly=True)
    x_cc_vehicle_id = fields.Many2one('account.analytic.account', string='Vehicle Cost Centre',
                                      readonly=True)
    x_cc_employee_id = fields.Many2one('account.analytic.account', string='Employee Cost Centre',
                                       readonly=True)
    x_driver = fields.Char(
        string='Driver', readonly=True,
        help='Our driver who delivered it. Empty when the customer collected it '
             'with his own tanker.')


    def _picking_form_ref(self):
        if self.picking_id.sudo().x_is_water_delivery:
            return 'KSW_water_delivery.view_water_delivery_note_form'
        return super()._picking_form_ref()


class KswStockMovementWizard(models.TransientModel):
    _inherit = 'ksw.stock.movement.wizard'

    def _line_vals(self):
        vals_list = super()._line_vals()
        picking_ids = {v['picking_id'] for v in vals_list if v.get('picking_id')}
        if not picking_ids:
            return vals_list
        info = {}
        for picking in self.env['stock.picking'].sudo().browse(list(picking_ids)):
            if picking.x_is_water_delivery:
                vehicle, branch, employee = picking._water_cost_centres()
                info[picking.id] = {
                    'x_dispatcher': picking.x_dispatch_id.dispatcher_id.name or False,
                    'x_driver': picking.x_driver_id.name or False,
                    'x_cc_branch_id': branch.id or False,
                    'x_cc_vehicle_id': vehicle.id or False,
                    'x_cc_employee_id': employee.id or False,
                }
        for vals in vals_list:
            vals.update(info.get(vals.get('picking_id'), {}))
        return vals_list
