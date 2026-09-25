"""Give the remaining pay components their BAS accounts.

19.0.4.15.0 seeded only the six that appear in the July 2026 voucher and
left the rest for the accountant, on the principle that a guessed account
is worse than an export that stops and asks. He answered: everything else
is `مصروفات اضافي العاملين` (3201010006) too — meals, the project
allowance, every bonus and the catch-all *Other*.

Archived components are included on purpose (``active_test=False``): an
old one can still be named on an entry in a payable batch, and it would
block the whole month's export from a record nobody can see in the list.

Only where the field is still empty, so an accountant's own value is never
overwritten and running it twice changes nothing.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

EXPENSE = ('3201010006', 'مصروفات اضافي العاملين')
ACCRUAL = ('2107010001', 'مصروفات مستحقة عمولات سائقين التريلات')


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    components = env['ksw.pay.component'].with_context(
        active_test=False).search([])
    filled = 0
    for component in components:
        vals = {}
        if not component.x_bas_expense_code:
            vals['x_bas_expense_code'] = EXPENSE[0]
            vals['x_bas_expense_name'] = EXPENSE[1]
        if not component.x_bas_accrual_code:
            vals['x_bas_accrual_code'] = ACCRUAL[0]
            vals['x_bas_accrual_name'] = ACCRUAL[1]
        if vals:
            component.write(vals)
            filled += 1
    _logger.info('KSW_commissions: BAS journal accounts filled in on %s '
                 'more pay components.', filled)
