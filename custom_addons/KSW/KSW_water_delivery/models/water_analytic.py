"""Cost centres on water sales (Oct 2026).

Each invoice line of a water delivery note carries the note's cost centres,
the way BAS tags a sale (COST_CENTER, COST_CENTER2):

* **Cost Centres** plan: the tanker that delivered it, or, when no tanker of
  ours was involved (the customer collected with his own), the branch;
* **Employee Cost Centre** plan: the driver who delivered it.

Branch and tanker are both in the Cost Centres plan, as in BAS's single
cost-centre column, so a line carries one of the two there; carrying both
would count the sale twice in that plan. The branch is visible on every sale
anyway through its own Sales Account.

Stamped on the note's sale order line when the note is ISSUED, so a later
change to a tanker's or driver's cost centre never re-tags sales already made
(a note records what was true that day). Odoo copies the order line's
distribution onto the invoice line, so month-end invoices (one line per note)
and cash invoices carry the same tags. Notes issued before this existed are
tagged at invoicing time instead.
"""
from odoo import api, fields, models


class StockPickingType(models.Model):
    _inherit = 'stock.picking.type'

    x_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Cost Centre', copy=False,
        help='The branch\'s cost centre. A sale collected by the customer\'s own '
             'tanker is booked to it; one our tanker delivered goes to the tanker\'s.')


class KswFleetVehicle(models.Model):
    _inherit = 'ksw.fleet.vehicle'

    # Same field as KSW_workshop_stock's (that module charges spare parts to
    # it); declared here too so water sales do not depend on the workshop.
    x_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Cost Centre',
        help='BAS cost centre of this vehicle. Water it delivers, and spare parts '
             'issued to it in the workshop, are charged here.')


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    # Stored on hr.employee, so HR-gated (KSW gotcha #32): a non-HR user's own
    # employee record is fetched through hr.employee.public, which lacks it.
    x_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Cost Centre', copy=False,
        groups='hr.group_hr_user',
        help='This employee\'s own cost centre (Employee Cost Centre plan). Water '
             'he delivers as a driver is tagged with it.')


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def _water_analytic_distribution(self):
        """{"<cost centre>,<employee cost centre>": 100} for this note, or
        False when it has neither."""
        self.ensure_one()
        note = self.sudo()
        cost_centre = note.x_vehicle_id.x_analytic_account_id \
            or (not note.x_vehicle_id and note.picking_type_id.x_analytic_account_id)
        employee = note.x_driver_id.x_analytic_account_id
        accounts = [a for a in (cost_centre, employee) if a]
        # One account per root plan in a key: two from the same plan would be
        # two keys, each at 100 %, counting the sale twice there.
        seen, ids = set(), []
        for account in accounts:
            if account.root_plan_id.id not in seen:
                seen.add(account.root_plan_id.id)
                ids.append(str(account.id))
        return {','.join(ids): 100.0} if ids else False


    def _water_stamp_analytic(self):
        for note in self.sudo():
            distribution = note._water_analytic_distribution()
            lines = note.sale_id.order_line.filtered(lambda l: not l.display_type)
            if distribution and lines:
                lines.write({'analytic_distribution': distribution})

    def _issue_water_note(self, payload, location_mode='raise'):
        picking = super()._issue_water_note(payload, location_mode=location_mode)
        picking._water_stamp_analytic()
        return picking


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    def _prepare_invoice_line(self, **optional_values):
        res = super()._prepare_invoice_line(**optional_values)
        if self.display_type or res.get('analytic_distribution') \
                or 'analytic_distribution' in optional_values:
            return res
        note = self.order_id.picking_ids.filtered('x_is_water_delivery')[:1]
        if not note:
            return res
        # Odoo 19's sale does not put the order line's distribution in these
        # values (it lands later through _related_analytic_distribution), so
        # the stamp is read here, and only a note issued before stamping
        # existed is tagged from today's cost centres.
        distribution = self.analytic_distribution or note._water_analytic_distribution()
        if distribution:
            res['analytic_distribution'] = distribution
        return res
