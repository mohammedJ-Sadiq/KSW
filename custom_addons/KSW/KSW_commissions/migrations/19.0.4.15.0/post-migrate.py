"""Fill in the BAS accounts the hand-typed journal entry already used.

The catalog records live in a ``noupdate="1"`` block, so the accounts added
to ``pay_component_data.xml`` in this version reach a fresh install and
nothing else (Odoo 19 Pitfall #2). This writes them onto the components an
existing database already has.

Only where the field is still empty: an accountant who has already set one
by hand owns it from then on. Running it twice changes nothing.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

# Read off the July 2026 voucher (قيد 26007264) — the entry this export
# replaces. Components not listed there are left for the accountant: a
# guessed account is worse than an export that stops and asks.
ACCOUNTS = {
    'OT': ('3201010006', 'مصروفات اضافي العاملين', False),
    'FRIDAY': ('3201010006', 'مصروفات اضافي العاملين', False),
    'ALW_DATA_ENTRY': ('3201010006', 'مصروفات اضافي العاملين', False),
    'ALW_LOCATION': ('3201010005', 'مصروفات بدل طبيعة عمل', False),
    'ALW_MOBILE': ('3201010004', 'مصروفات بدل اتصال (جوال)', False),
    'TRIPS': ('3203020007', 'حساب مصروفات - عمولات سائقين التريلات', True),
}
ACCRUAL = ('2107010001', 'مصروفات مستحقة عمولات سائقين التريلات')


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    Component = env['ksw.pay.component'].with_context(active_test=False)
    filled = 0
    for code, (expense, name, cost_centre) in ACCOUNTS.items():
        component = Component.search([('code', '=', code)], limit=1)
        if not component:
            continue
        vals = {}
        if not component.x_bas_expense_code:
            vals['x_bas_expense_code'] = expense
            vals['x_bas_expense_name'] = name
            vals['x_bas_use_cost_center'] = cost_centre
        if not component.x_bas_accrual_code:
            vals['x_bas_accrual_code'] = ACCRUAL[0]
            vals['x_bas_accrual_name'] = ACCRUAL[1]
        if vals:
            component.write(vals)
            filled += 1
    _logger.info('KSW_commissions: BAS journal accounts set on %s pay '
                 'components.', filled)
