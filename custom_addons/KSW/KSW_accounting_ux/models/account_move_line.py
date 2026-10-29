from collections import defaultdict

from odoo import api, fields, models


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    # The two BAS cost-centre dimensions as plain columns. The distribution
    # widget shows both in one cell, and only to Analytic Accounting users; a
    # line carries at most one of each (BAS tags one per column), so these
    # can be shown, sorted, grouped and filtered like any field. A split
    # distribution shows its largest share.
    x_cost_centre_id = fields.Many2one(
        'account.analytic.account', string='Cost Centre', compute='_compute_ksw_cost_centres',
        store=True, index=True, help='Vehicle or department (BAS COST_CENTER).')
    x_employee_cost_centre_id = fields.Many2one(
        'account.analytic.account', string='Employee Cost Centre',
        compute='_compute_ksw_cost_centres', store=True, index=True,
        help='The driver or employee (BAS COST_CENTER2, «مركز تكلفة الموظف»).')

    @api.depends('analytic_distribution')
    def _compute_ksw_cost_centres(self):
        lines = self.filtered('analytic_distribution')
        (self - lines).update({'x_cost_centre_id': False, 'x_employee_cost_centre_id': False})
        if not lines:
            return
        ids = {int(i) for line in lines for key in line.analytic_distribution for i in key.split(',')
               if i.isdigit()}
        Account = self.env['account.analytic.account'].sudo().with_context(active_test=False)
        root = {a.id: a.root_plan_id.x_bas_code for a in Account.browse(ids).exists()}
        for line in lines:
            share = defaultdict(lambda: defaultdict(float))
            for key, pct in line.analytic_distribution.items():
                for i in key.split(','):
                    if i.isdigit() and int(i) in root:
                        share[root[int(i)]][int(i)] += pct or 0.0
            pick = {plan: max(acc, key=acc.get) for plan, acc in share.items()}
            # Vehicles and branches are top-level plans of their own since Oct
            # 2026 (BAS codes 14 and 12); BAS's COST_CENTER is the vehicle when
            # there is one, else the department, else the branch.
            line.x_cost_centre_id = pick.get('14') or pick.get('WREF10') or pick.get('12') or False
            line.x_employee_cost_centre_id = pick.get('WREF10-EMP', False)

    def _get_computed_taxes(self):
        """Fall back to the company's default sale/purchase tax.

        With no product, core takes the tax only from the account's default
        taxes.  The BAS chart was imported without any, so a bill typed against
        3101010001 posted with no VAT at all, while BAS books the same purchase
        with 15% input VAT.  The company default (Settings > Default Taxes) is
        what the chart template would have stamped on its own accounts.
        """
        taxes = super()._get_computed_taxes()
        if taxes or self.product_id or self.account_id.tax_ids:
            return taxes
        move = self.move_id
        if move.is_purchase_document(include_receipts=True):
            taxes = move.company_id.account_purchase_tax_id
        elif move.is_sale_document(include_receipts=True):
            taxes = move.company_id.account_sale_tax_id
        if taxes and move.fiscal_position_id:
            taxes = move.fiscal_position_id.map_tax(taxes)
        return taxes
