"""Whole riyals, and one line per pay type on the BAS journal.

Two things the code change alone does not reach:

* The Friday allowance is described on the voucher as
  «بدل عمل ايام الجمعة» — the accountant's wording, not the catalog's
  «بدل العمل يوم الجمعة». Set only where the field is still empty.

* A register built before the rounding rule still holds fractional
  earnings, and the journal now refuses it as "a figure the entries no
  longer add up to". Approved, unpaid months are re-derived with the same
  rule (``_rounded_component_totals``) — but only a line whose unrounded
  entries still equal what the register says, so a line that disagrees
  for any other reason is left for the export to name. A paid month is
  never touched: its money has left the bank.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})

    friday = env.ref('KSW_commissions.pay_component_friday',
                     raise_if_not_found=False)
    if friday and not friday.x_bas_ref:
        friday.x_bas_ref = 'بدل عمل ايام الجمعة'

    for run in env['ksw.pay.run'].search([('state', '=', 'approved')]):
        entries = env['ksw.pay.entry'].browse()
        for batch in run._payable_batches(settled_only=True):
            entries |= batch.entry_ids
        entries = entries.filtered('employee_id')
        payable = entries - run._held_entries(entries)
        raw = {}
        for entry in payable:
            raw[entry.employee_id.id] = (raw.get(entry.employee_id.id, 0.0)
                                         + (entry.amount or 0.0))
        rounded = run._rounded_component_totals(payable)

        changed = skipped = 0
        for line in run.line_ids:
            emp = line.employee_id.id
            if emp not in raw or abs(raw[emp] - (line.earnings or 0.0)) >= 0.005:
                continue
            whole = sum(rounded[emp].values())
            if whole == line.earnings:
                continue
            if whole < (line.loan_offset or 0.0):
                skipped += 1
                _logger.warning(
                    'KSW_commissions: %s line %s not rounded — %.2f would '
                    'fall below the settled loan offset %.2f.',
                    run.display_name, line.id, whole, line.loan_offset)
                continue
            line.earnings = whole
            changed += 1
        _logger.info('KSW_commissions: %s — %d register lines rounded to '
                     'whole riyals, %d skipped.', run.display_name,
                     changed, skipped)
