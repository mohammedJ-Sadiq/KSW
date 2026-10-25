import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Drop the 0/0 lines BAS header slots left on imported vouchers.

    A BAS sales voucher always writes its header slots (INVACCPOS 1 customer,
    3 default sales account); when the money is booked on item / customer
    detail slots instead, those rows stay at AMOUNT 0.  The importer let them
    through because TAX_AMOUNT is repeated on every row.  They carry no amount,
    no analytic line and no reconciliation -- only noise on the entry and in
    every ledger.  force_delete still runs core's lock-date and hash checks.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    lines = env['account.move.line'].search([
        ('move_id.x_bas_key', '!=', False),
        ('display_type', '=', 'product'),
        ('debit', '=', 0), ('credit', '=', 0), ('amount_currency', '=', 0),
        # not `reconciled`: a receivable line with nothing to pay computes as
        # reconciled without any matching.  What must not be broken is a match.
        ('full_reconcile_id', '=', False), ('matched_debit_ids', '=', False),
        ('matched_credit_ids', '=', False),
    ])
    moves = lines.move_id
    lines.with_context(force_delete=True).unlink()
    _logger.info('BAS import: removed %s zero-value lines from %s entries',
                 len(lines), len(moves))
