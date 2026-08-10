"""19.0.2.0.0 — move entry-type contributions from scalar columns to lines.

Until now each entry type wrote into its own scalar column on
``ksw.commission.sheet`` (``driver_commission_amount``,
``location_allowance_amount``, ``sales_commission_amount``,
``collection_commission_amount``, ``combined_commission_amount``) and
``total`` was the sum of those plus the manual lines.

From this version a contribution is a tagged ``ksw.commission.sheet.line``
(``is_auto=True``), ``total`` is simply the sum of every line, and the five
scalars are *derived* from those lines (kept as deprecated shims so the PDF
reports, the bank export and the list-view ``sum=`` columns keep working).

This script recreates the missing lines so historical sheets keep exactly the
totals they had. The columns are deliberately **not** dropped — that makes the
whole change revertible by reverting code.

Safety properties:

* **Idempotent** — a sheet that already has an auto line for a given
  ``(source_model, source_key)`` is skipped, so a re-run is harmless.
* **Never loses money** — when the originating entry line can no longer be
  found (its sheet was reset to draft, or deleted), the contribution is still
  recreated with ``source_id = 0`` and logged as a WARNING for review.
* **Verified** — every sheet's recomputed total is compared against the value
  it had before, and any drift is logged with the sheet name.

The scalar values are read from the snapshot ``pre-migrate.py`` took in raw
SQL, not from the live columns: by now the ORM has loaded the new definitions
in which those same fields are computed from the lines we are creating, so the
live columns can no longer be trusted.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

# Kept in step with pre-migrate.py (migration scripts are not a package, so
# this cannot be imported).
SNAPSHOT_TABLE = 'ksw_commission_sheet_scalar_snapshot_19_2_0_0'

# scalar column -> (source line model, contributing field, category xmlid, key)
SPECS = [
    ('driver_commission_amount', 'ksw.driver.commission.line',
     'total_commission', 'KSW_commissions.cat_vehicle_commission', 'main'),
    ('location_allowance_amount', 'ksw.location.allowance.line',
     'total_allowance', 'KSW_commissions.cat_location', 'main'),
    ('sales_commission_amount', 'ksw.sales.commission.line',
     'sales_commission_amount',
     'KSW_commissions.cat_sales_commission', 'sales'),
    ('collection_commission_amount', 'ksw.sales.commission.line',
     'collection_commission_amount',
     'KSW_commissions.cat_collection_commission', 'collection'),
    ('combined_commission_amount', 'ksw.sales.commission.line',
     'combined_commission_amount',
     'KSW_commissions.cat_combined_commission', 'combined'),
]


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})

    cr.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
        (SNAPSHOT_TABLE,),
    )
    if not cr.fetchone():
        _logger.info("KSW_commissions 19.0.2.0.0: no pre-migrate snapshot — "
                     "nothing to migrate.")
        return

    cr.execute("SELECT * FROM %s" % SNAPSHOT_TABLE)
    rows = cr.dictfetchall()
    if not rows:
        _logger.info("KSW_commissions 19.0.2.0.0: no scalar contributions "
                     "to migrate.")
        cr.execute("DROP TABLE %s" % SNAPSHOT_TABLE)
        return

    Sheet = env['ksw.commission.sheet']
    Line = env['ksw.commission.sheet.line'].with_context(_ksw_auto_sync=True)
    old_totals = {row['id']: row['total'] or 0.0 for row in rows}

    # Contributions already migrated (makes the script re-runnable).
    already = set()
    for line in Line.search([('is_auto', '=', True),
                            ('sheet_id', 'in', list(old_totals))]):
        already.add((line.sheet_id.id, line.source_model, line.source_key))

    to_create = []
    orphans = 0
    for row in rows:
        sheet = Sheet.browse(row['id'])
        for column, line_model, amount_field, category_xmlid, key in SPECS:
            amount = row[column] or 0.0
            if not amount:
                continue
            if (sheet.id, line_model, key) in already:
                continue

            # Same lookup the pre-2.0.0 compute used, so we re-link to the
            # record that actually produced the number where it still exists.
            source = env[line_model].search([
                ('employee_id', '=', sheet.employee_id.id),
                ('sheet_id.period', '=', sheet.period),
                ('sheet_id.state', '=', 'confirmed'),
            ], limit=1)
            if not source:
                orphans += 1
                _logger.warning(
                    "KSW_commissions 19.0.2.0.0: sheet %s (%s, %s) carries "
                    "%s = %.2f but no %s remains to link it to. Recreating "
                    "the line with source_id=0 so the amount is preserved — "
                    "review this sheet.",
                    sheet.name, sheet.employee_id.display_name, sheet.period,
                    column, amount, line_model,
                )

            to_create.append({
                'sheet_id': sheet.id,
                'category_id': env.ref(category_xmlid).id,
                'amount': amount,
                'description': env.ref(category_xmlid).name,
                'is_auto': True,
                'source_model': line_model,
                'source_id': source.id if source else 0,
                'source_key': key,
            })

    if to_create:
        Line.create(to_create)
    _logger.info(
        "KSW_commissions 19.0.2.0.0: created %s contribution lines across "
        "%s sheets (%s with no surviving source).",
        len(to_create), len(rows), orphans,
    )

    # The compute signature changed, so the ORM will not recompute on its own.
    sheets = Sheet.browse(list(old_totals))
    sheets.modified(['line_ids'])
    sheets.flush_recordset(
        ['lines_subtotal', 'auto_subtotal', 'total', 'total_payable'])

    drifted = [
        (s.name, old_totals[s.id], s.total)
        for s in sheets
        if abs((s.total or 0.0) - old_totals[s.id]) >= 0.01
    ]
    if drifted:
        _logger.error(
            "KSW_commissions 19.0.2.0.0: %s sheet(s) changed total during "
            "migration — REVIEW THESE: %s",
            len(drifted),
            ', '.join('%s (%.2f -> %.2f)' % d for d in drifted[:20]),
        )
    else:
        _logger.info("KSW_commissions 19.0.2.0.0: all %s sheet totals "
                     "reproduced exactly.", len(sheets))

    # Keep the snapshot when something looked wrong, so it can be inspected.
    if not drifted and not orphans:
        cr.execute("DROP TABLE %s" % SNAPSHOT_TABLE)
    else:
        _logger.warning(
            "KSW_commissions 19.0.2.0.0: keeping table %s for inspection.",
            SNAPSHOT_TABLE)
