import logging

from odoo import api, models

_logger = logging.getLogger(__name__)

# BAS charges vehicle spare parts to 3102020001 «حساب مصروفات قطع غيار السيارات»
# (2025-2026: every parts line of the workshop custodies). BAS keeps no stock of
# them, so the stock account is the closest BAS one: 1206010002 «مواد خام وقطع
# غيار قسم الصيانة بالمستودع».
VEHICLE_PARTS_ACCOUNTS = ('1206010002', '3102020001')


class ProductCategory(models.Model):
    _inherit = 'product.category'

    @api.model
    def _ksw_setup_vehicle_parts(self):
        """Valuation and accounts of «قطع غيار السيارات», like the other BAS item classes.

        Keeps an accountant's later choice: a BAS stock account (1206…) or any
        issue account already set is left alone; Odoo's company-wide default
        stock account (131100) is not a choice and is replaced. Skipped
        (and logged) where the BAS chart is not loaded.
        """
        categ = self.env.ref('KSW_workshop_stock.categ_vehicle_parts', raise_if_not_found=False)
        if not categ:
            return
        for company in self.env['res.company'].search([]):
            Account = self.env['account.account'].with_company(company)
            stock_acc, issue_acc = (Account.search([('code', '=', c)], limit=1) for c in VEHICLE_PARTS_ACCOUNTS)
            if not stock_acc or not issue_acc:
                _logger.info('Vehicle spare parts: BAS accounts missing for %s, accounts left unset.', company.name)
                continue
            categ = categ.with_company(company)
            vals = {}
            if categ.property_valuation != 'real_time':
                vals['property_valuation'] = 'real_time'
            if categ.property_cost_method != 'average':
                vals['property_cost_method'] = 'average'
            if not (categ.property_stock_valuation_account_id.code or '').startswith('1206'):
                vals['property_stock_valuation_account_id'] = stock_acc.id
            if not categ.property_account_expense_categ_id:
                vals['property_account_expense_categ_id'] = issue_acc.id
            if vals:
                categ.write(vals)
            # Bought on purchase orders with VAT, like the other stock items.
            tax = self.env['account.tax'].search([
                ('company_id', '=', company.id), ('type_tax_use', '=', 'purchase'),
                ('amount', '=', 15.0), ('amount_type', '=', 'percent'),
                ('price_include', '=', False), ('name', '=', '15%')], order='id', limit=1)
            if tax:
                products = self.env['product.template'].with_context(active_test=False).search([
                    ('categ_id', '=', categ.id), ('supplier_taxes_id', '=', False)])
                products.write({'supplier_taxes_id': [(4, tax.id)]})
