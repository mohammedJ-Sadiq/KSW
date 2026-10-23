from odoo import api, fields, models
from odoo.exceptions import UserError


class StockWarehouse(models.Model):
    _inherit = 'stock.warehouse'

    x_bas_code = fields.Char(
        'BAS Code', copy=False, index=True,
        help="Warehouse code in BAS (WSHOW.CODE), e.g. 15. Documents of this "
             "warehouse carry the branch code 1 + it (115), showrooms 2 + it.")
    x_bas_kind = fields.Selection(
        [('warehouse', 'Warehouse'), ('showroom', 'Showroom')], 'BAS Type', copy=False)


class StockLocation(models.Model):
    _inherit = 'stock.location'

    x_ksw_consumption = fields.Boolean(
        'Consumption Location',
        help="Stock sent here is used up (BAS 101 «فاتورة صرف»). The entry "
             "debits the issue's account instead of this location's.")


class StockPickingType(models.Model):
    _inherit = 'stock.picking.type'

    x_ksw_issue = fields.Boolean(
        'Stock Issue',
        help="Issue of stock for use: posts Dr issue account / Cr stock "
             "account when validated, and requires a cost centre.")


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    x_ksw_issue = fields.Boolean(related='picking_type_id.x_ksw_issue')
    # Document flow, both ways. The accounting document of a transfer: for a
    # receipt, the bill it was invoiced on (the BAS purchase with the same key,
    # or the bills of its Odoo purchase order); for a stock issue, the BAS
    # issue voucher (101) with the same key. account.move.x_picking_ids is the
    # way back.
    x_vendor_bill_ids = fields.Many2many('account.move', string='Vendor Bills',
                                         compute='_compute_vendor_bills', compute_sudo=True)
    x_vendor_bill_id = fields.Many2one('account.move', string='Vendor Bill',
                                       compute='_compute_vendor_bills', compute_sudo=True)
    x_vendor_bill_count = fields.Integer(compute='_compute_vendor_bills', compute_sudo=True)
    x_issue_account_id = fields.Many2one(
        'account.account', 'Issue Account', check_company=True,
        domain=[('account_type', 'not in', ('asset_receivable', 'liability_payable',
                                            'asset_cash', 'liability_credit_card', 'off_balance'))],
        help="Account debited for what is issued. Leave empty to use each "
             "item class's expense account (tyres 3102020002, desalination "
             "parts 3102010001). Set it to charge an employee, a project or a "
             "branch instead, as BAS does with 1207 / 1212 / 2104 accounts.")

    def _compute_vendor_bills(self):
        keys = [k for k in self.mapped('x_bas_key') if k and k[:4] in ('006/', '101/')]
        bas = {}
        if keys:
            for move in self.env['account.move'].search([('x_bas_key', 'in', keys),
                                                         ('state', '=', 'posted')]):
                bas.setdefault(move.x_bas_key, move)
        for picking in self:
            bills = self.env['account.move']
            if picking.x_bas_key in bas:
                bills |= bas[picking.x_bas_key]
            if 'purchase_id' in picking._fields and picking.purchase_id:
                bills |= picking.purchase_id.invoice_ids.filtered(lambda m: m.state == 'posted')
            picking.x_vendor_bill_ids = bills
            picking.x_vendor_bill_id = bills[:1]
            picking.x_vendor_bill_count = len(bills)

    def action_view_vendor_bills(self):
        self.ensure_one()
        bills = self.x_vendor_bill_ids
        action = {'type': 'ir.actions.act_window', 'res_model': 'account.move',
                  'name': self.env._('Vendor Bills')}
        if len(bills) == 1:
            action.update(view_mode='form', res_id=bills.id)
        else:
            action.update(view_mode='list,form', domain=[('id', 'in', bills.ids)])
        return action

    def button_validate(self):
        for picking in self.filtered('x_ksw_issue'):
            if not picking.location_dest_id.x_ksw_consumption:
                raise UserError(self.env._(
                    "Stock issue %(name)s must go to a consumption location, not %(loc)s.",
                    name=picking.name, loc=picking.location_dest_id.display_name))
            missing = picking.move_ids.filtered(lambda m: m.quantity and not m.analytic_distribution)
            if missing:
                raise UserError(self.env._(
                    "Set a cost centre on every line of %(name)s before validating: %(items)s.",
                    name=picking.name, items=", ".join(missing.product_id.mapped('display_name'))))
        return super().button_validate()


class StockMove(models.Model):
    _inherit = 'stock.move'

    def _ksw_issue_account(self):
        """Account a stock issue debits: the issue's own, else the item class's."""
        self.ensure_one()
        return (self.picking_id.x_issue_account_id
                or self.product_id.categ_id.property_account_expense_categ_id
                or self.location_dest_id.valuation_account_id
                or self.location_id.valuation_account_id)

    def _get_account_move_line_vals(self):
        """Issue into / return from a consumption location: swap the location's
        placeholder account for the issue account. The stock side is untouched,
        and stock_analytic has already put the cost centre on the other line."""
        res = super()._get_account_move_line_vals()
        consumption = self.location_dest_id if self.location_dest_id.x_ksw_consumption else (
            self.location_id if self.location_id.x_ksw_consumption else False)
        if not consumption:
            return res
        placeholder = consumption.valuation_account_id.id
        target = self._ksw_issue_account()
        for vals in res:
            if vals['account_id'] == placeholder and target:
                vals['account_id'] = target.id
        return res

    @api.model
    def _ksw_issue_domain(self):
        return [('picking_type_id.x_ksw_issue', '=', True)]


class AccountMove(models.Model):
    _inherit = 'account.move'

    # The way back from a bill or an issue voucher to its stock transfer(s).
    x_picking_ids = fields.Many2many('stock.picking', string='Transfers',
                                     compute='_compute_ksw_pickings', compute_sudo=True)
    x_picking_count = fields.Integer(compute='_compute_ksw_pickings', compute_sudo=True)

    def _compute_ksw_pickings(self):
        keys = [k for k in self.mapped('x_bas_key') if k and k[:4] in ('006/', '101/')]
        by_key = {}
        if keys:
            for picking in self.env['stock.picking'].search([('x_bas_key', 'in', keys)]):
                by_key.setdefault(picking.x_bas_key, self.env['stock.picking'])
                by_key[picking.x_bas_key] |= picking
        for move in self:
            pickings = by_key.get(move.x_bas_key, self.env['stock.picking'])
            pickings |= move.invoice_line_ids.purchase_line_id.move_ids.picking_id
            move.x_picking_ids = pickings
            move.x_picking_count = len(pickings)

    def action_view_ksw_pickings(self):
        self.ensure_one()
        pickings = self.x_picking_ids
        action = {'type': 'ir.actions.act_window', 'res_model': 'stock.picking',
                  'name': self.env._('Transfers')}
        if len(pickings) == 1:
            action.update(view_mode='form', res_id=pickings.id)
        else:
            action.update(view_mode='list,form', domain=[('id', 'in', pickings.ids)])
        return action
