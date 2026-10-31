"""The branch master is the warehouse (Oct 2026).

A water branch used to be its delivery operation type, holding the branch's
code, sales account, cost centre and cash settings, and sitting in the main
warehouse; client lists and revenue rules joined it by a typed code. Now the
branch is its WAREHOUSE (W20 = Nabiya, branch 120), as Odoo models a physical
depot, and holds what describes the branch. The water operation type keeps
only the delivery process (numbering, signature, location rule, issue mode,
dispatchers) and reads the branch fields from its warehouse, under the same
names, so every caller is unchanged.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class StockWarehouse(models.Model):
    _inherit = 'stock.warehouse'

    x_branch_code = fields.Char(
        string='Branch Code', index=True, copy=False,
        help='Short code of the branch (the same code BAS uses, e.g. 120 for '
             'Nabiya). Ties the branch to its imported history and client list.')
    x_sales_account_id = fields.Many2one(
        'account.account', string='Sales Account', copy=False,
        domain="[('account_type', 'in', ('income', 'income_other'))]",
        help='The income account this branch\'s sales are booked to. A product line '
             'in Revenue Accounts for this branch takes precedence.')
    x_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Branch Cost Centre', copy=False,
        help='The branch\'s cost centre (Branch Cost Centre plan). Every sale of the '
             'branch is tagged with it.')
    x_cash_partner_id = fields.Many2one(
        'res.partner', string='Cash Customer', copy=False,
        help='The contact this branch\'s cash sales are made out to. Its pricelist '
             'holds the branch\'s cash prices (Client Rates).')
    x_cash_journal_id = fields.Many2one(
        'account.journal', string='Cash Till', copy=False,
        domain="[('type', '=', 'cash')]",
        help='Where the dispatcher\'s cash goes: the branch till.')
    x_cash_sale_journal_id = fields.Many2one(
        'account.journal', string='Cash Sales Journal', copy=False,
        domain="[('type', '=', 'sale')]",
        help='The journal cash sales are invoiced in. Empty uses the company\'s '
             'Cash Sales (CSAL) journal.')
    x_water_picking_type_ids = fields.One2many(
        'stock.picking.type', 'warehouse_id', string='Water Delivery Settings',
        domain=[('x_is_water_delivery', '=', True)])

    def action_create_cash_customer(self):
        return self.x_water_picking_type_ids[:1].action_create_cash_customer() \
            if self.x_water_picking_type_ids else False


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # The branches that deliver to this customer, on the customer (SAP keeps
    # the delivering plant on the customer). The rows stay in
    # ksw.water.client.branch, which the BAS import fills and Water Delivery
    # reads; this is the customer's view of them.
    x_water_branch_ids = fields.Many2many(
        'stock.warehouse', string='Delivering Branches',
        compute='_compute_x_water_branch_ids', inverse='_inverse_x_water_branch_ids',
        compute_sudo=True,
        domain="[('x_branch_code', '!=', False)]",
        help='The branches whose drivers deliver to this customer. Empty: every '
             'branch may.')

    def _compute_x_water_branch_ids(self):
        Coverage = self.env['ksw.water.client.branch'].sudo()
        rows = Coverage.search([('partner_id', 'in', self.ids)])
        Warehouse = self.env['stock.warehouse'].sudo()
        by_code = {w.x_branch_code: w for w in Warehouse.search([('x_branch_code', '!=', False)])}
        for partner in self:
            codes = rows.filtered(lambda r: r.partner_id == partner).mapped('branch_code')
            partner.x_water_branch_ids = Warehouse.union(*[by_code[c] for c in codes if c in by_code])

    def _inverse_x_water_branch_ids(self):
        Coverage = self.env['ksw.water.client.branch'].sudo().with_context(active_test=False)
        for partner in self:
            wanted = set(partner.x_water_branch_ids.mapped('x_branch_code')) - {False}
            rows = Coverage.search([('partner_id', '=', partner.id)])
            have = {r.branch_code: r for r in rows}
            for code, row in have.items():
                if code in wanted and not row.active:
                    row.active = True
                elif code not in wanted and row.active:
                    row.active = False
            Coverage.create([{'partner_id': partner.id, 'branch_code': code}
                             for code in wanted - set(have)])


class KswWaterClientBranch(models.Model):
    _inherit = 'ksw.water.client.branch'

    warehouse_id = fields.Many2one(
        'stock.warehouse', string='Branch', compute='_compute_warehouse_id', store=True,
        index=True, help='The branch this code belongs to.')

    @api.depends('branch_code')
    def _compute_warehouse_id(self):
        _assign_warehouse_by_code(self)


class KswWaterRevenueAccount(models.Model):
    _inherit = 'ksw.water.revenue.account'

    # The branch is the key accountants choose; branch_code follows it, so
    # every existing lookup by code keeps working.
    warehouse_id = fields.Many2one(
        'stock.warehouse', string='Branch', compute='_compute_warehouse_id', store=True,
        readonly=False, inverse='_inverse_warehouse_id', index=True,
        domain="[('x_branch_code', '!=', False)]",
        help='The branch whose sales of this product post to this account.')
    branch_code = fields.Char(required=False)
    branch_default_account_id = fields.Many2one(
        related='warehouse_id.x_sales_account_id', string='Branch Default',
        help='What this branch posts to for products with no line here. Set on '
             'the branch (Inventory > Warehouses > Branch).')

    @api.depends('branch_code')
    def _compute_warehouse_id(self):
        _assign_warehouse_by_code(self)

    def _inverse_warehouse_id(self):
        for rec in self.filtered('warehouse_id'):
            code = rec.warehouse_id.sudo().x_branch_code
            if not code:
                raise UserError(_('%s has no branch code yet: set it on the '
                                  'branch first.', rec.warehouse_id.display_name))
            if rec.branch_code != code:
                rec.branch_code = code

    @api.constrains('branch_code', 'warehouse_id')
    def _check_branch(self):
        if self.filtered(lambda r: not r.branch_code and not r.warehouse_id):
            raise UserError(_('Choose the branch.'))


def _assign_warehouse_by_code(records):
    Warehouse = records.env['stock.warehouse'].sudo()
    codes = set(records.mapped('branch_code')) - {False}
    by_code = {w.x_branch_code: w.id for w in Warehouse.search([('x_branch_code', 'in', list(codes))])}
    for rec in records:
        rec.warehouse_id = by_code.get(rec.branch_code, False)
