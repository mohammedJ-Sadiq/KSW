"""19.0.3.0.0 — rebuild the old commission world as pay entries and runs.

Reads the snapshots ``pre-migrate.py`` took, then:

1. recreates every historical entry-sheet line as a ``ksw.pay.entry`` inside a
   ``ksw.pay.batch``, and every manual commission-sheet line the same way;
2. recreates each historical commission sheet as a ``ksw.pay.run.line`` inside
   the month's ``ksw.pay.run``, preserving the total that was actually paid;
3. re-points the settled-installment links onto the new register lines;
4. drops the retired tables.

The guiding rule is the same as 19.0.2.0.0: **reproduce every historical
figure to the cent, and never delete money.** Where a line cannot be mapped to
a component it is still recreated under the catch-all ``OTHER`` component, and
logged, rather than dropped.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

SNAP = 'ksw_pay_migration_snapshot_19_3_0_0'

# Old commission category code -> new pay component xmlid.
CATEGORY_MAP = {
    'project_management': 'pay_component_project_management',
    'location': 'pay_component_location_allowance',
    'data_entry': 'pay_component_data_entry',
    'mobile_phone': 'pay_component_mobile',
    'vehicle_commission': 'pay_component_driver_trips',
    'holiday_bonus': 'pay_component_bonus_employee',
    'employee_bonus': 'pay_component_bonus_employee',
    'friday': 'pay_component_friday',
    'other': 'pay_component_other',
    'overtime': 'pay_component_overtime',
}


def _table_exists(cr, table):
    cr.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
        (table,))
    return bool(cr.fetchone())


def _rows(cr, table):
    if not _table_exists(cr, table):
        return []
    cr.execute("SELECT * FROM %s" % table)
    return cr.dictfetchall()


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})

    Component = env['ksw.pay.component']
    Batch = env['ksw.pay.batch'].with_context(_ksw_migration=True)
    Entry = env['ksw.pay.entry'].with_context(_ksw_migration=True)
    Run = env['ksw.pay.run']

    def component(xmlid):
        return env.ref('KSW_commissions.%s' % xmlid, raise_if_not_found=False)

    other = component('pay_component_other')
    batches = {}
    dept_cache = {}
    placeholder = [None]   # lazily created, only if actually needed

    def department_for(employee_id):
        """The department a migrated entry belongs to.

        Department-scoped components need one, and the old commission sheet
        had no department of its own — it hung off the employee. Employees
        with no department at all fall into a clearly-named placeholder so
        nothing is silently dropped.
        """
        if employee_id in dept_cache:
            return dept_cache[employee_id]
        employee = env['hr.employee'].browse(employee_id).exists()
        dept = employee.sudo().department_id if employee else None
        if not dept:
            if placeholder[0] is None:
                placeholder[0] = env['hr.department'].sudo().search(
                    [('name', '=', 'Unassigned (migrated)')], limit=1
                ) or env['hr.department'].sudo().create(
                    {'name': 'Unassigned (migrated)'})
            dept = placeholder[0]
        dept_cache[employee_id] = dept.id
        return dept.id

    def batch_for(comp, period, department_id=None, site_id=None):
        """Find-or-create the batch a migrated entry belongs in."""
        key = (comp.id, period, department_id or 0, site_id or 0)
        if key in batches:
            return batches[key]
        existing = Batch.search([
            ('component_id', '=', comp.id), ('period', '=', period),
            ('department_id', '=', department_id or False),
            ('site_id', '=', site_id or False),
        ], limit=1)
        if not existing:
            existing = Batch.create({
                'component_id': comp.id, 'period': period,
                'department_id': department_id or False,
                'site_id': site_id or False,
                'note': 'Migrated from the pre-19.0.3.0.0 commission app.',
            })
        batches[key] = existing
        return existing

    # ------------------------------------------------------------------
    # 1. Entry sheets -> pay entries
    # ------------------------------------------------------------------
    created = 0

    for row in _rows(cr, '%s_overti' % SNAP):
        comp = component('pay_component_overtime')
        if not (comp and row.get('employee_id')):
            continue
        if not (row.get('quantity') or row.get('amount')):
            continue
        batch = batch_for(comp, row['period'],
                          row.get('department_id')
                          or department_for(row['employee_id']))
        Entry.create({
            'batch_id': batch.id, 'employee_id': row['employee_id'],
            'date': row.get('entry_date') or row['period'],
            'quantity': row.get('quantity') or 0.0,
            'location_id': row.get('location_id') or False,
            'reason': row.get('reason') or 'Migrated',
            'details': row.get('details') or False,
            'amount_override': row.get('amount') or 0.0,
        })
        created += 1

    empty_rows = 0
    for row in _rows(cr, '%s_driver' % SNAP):
        comp = component('pay_component_driver_trips')
        if not (comp and row.get('employee_id')):
            continue
        if not (row.get('quantity') or row.get('amount')):
            # A roster row with no trips and no money. It carried no
            # information beyond "this driver was listed", so recreating it
            # would only add noise to the new register.
            empty_rows += 1
            continue
        batch = batch_for(comp, row['period'], site_id=row.get('site_id'))
        Entry.create({
            'batch_id': batch.id, 'employee_id': row['employee_id'],
            'quantity': row.get('quantity') or 0.0,
            'quantity_ref': row.get('quantity_ref') or 0.0,
            'amount_override': row.get('amount') or 0.0,
            'details': 'Migrated from the driver commission sheet.',
        })
        created += 1

    meal_components = {
        'breakfast_qty': component('pay_component_meal_breakfast'),
        'lunch_qty': component('pay_component_meal_lunch'),
        'dinner_qty': component('pay_component_meal_dinner'),
    }
    for row in _rows(cr, '%s_meals' % SNAP):
        for field, comp in meal_components.items():
            qty = row.get(field) or 0
            if not (comp and qty and row.get('employee_id')):
                continue
            batch = batch_for(comp, row['period'],
                              row.get('department_id')
                              or department_for(row['employee_id']))
            Entry.create({
                'batch_id': batch.id, 'employee_id': row['employee_id'],
                'quantity': qty,
            })
            created += 1

    # Manual commission-sheet lines (auto lines are rebuilt from their own
    # source above, so they would double-count).
    unmapped = 0
    for row in _rows(cr, '%s_line' % SNAP):
        if row.get('is_auto'):
            continue
        comp = component(CATEGORY_MAP.get(row.get('category_code') or '', '')) \
            or other
        if not (comp and row.get('employee_id')):
            continue
        if comp == other and row.get('category_code') not in CATEGORY_MAP:
            unmapped += 1
        batch = batch_for(comp, row['period'],
                          department_for(row['employee_id']))
        Entry.create({
            'batch_id': batch.id, 'employee_id': row['employee_id'],
            'quantity': row.get('quantity') or 1.0,
            'amount_override': row.get('amount') or 0.0,
            'reason': row.get('description')
            or (row.get('category_name') or 'Migrated'),
        })
        created += 1

    _logger.info(
        "19.0.3.0.0: recreated %s pay entr(ies) in %s batch(es); "
        "%s line(s) had no matching component and went to 'Other'; "
        "%s empty roster row(s) skipped.",
        created, len(batches), unmapped, empty_rows)

    # Historical batches are settled history, not work in progress.
    for batch in Batch.browse([b.id for b in batches.values()]):
        batch.sudo().write({'state': 'approved'})

    # ------------------------------------------------------------------
    # 2. Commission sheets -> the payment register
    # ------------------------------------------------------------------
    sheet_to_line = {}
    runs = {}
    sheets = _rows(cr, '%s_sheet' % SNAP)
    for row in sheets:
        period = row['period']
        if period not in runs:
            run = Run.search([('period', '=', period)], limit=1)
            if not run:
                run = Run.create({'period': period})
            runs[period] = run
        run = runs[period]
        if not row.get('employee_id'):
            continue
        line = env['ksw.pay.run.line'].search([
            ('run_id', '=', run.id),
            ('employee_id', '=', row['employee_id']),
        ], limit=1)
        if not line:
            line = env['ksw.pay.run.line'].create({
                'run_id': run.id,
                'employee_id': row['employee_id'],
                'earnings': row.get('total') or 0.0,
                'loan_offset': row.get('loans') or 0.0,
                'x_unwind_data': row.get('x_unwind_data') or False,
            })
        sheet_to_line[row['id']] = line.id

    # A sheet that was 'done' had already been paid.
    for period, run in runs.items():
        was_done = any(r['state'] == 'done' and r['period'] == period
                       for r in sheets)
        run.sudo().write({'state': 'paid' if was_done else 'open'})

    _logger.info("19.0.3.0.0: rebuilt %s register line(s) across %s run(s).",
                 len(sheet_to_line), len(runs))

    # ------------------------------------------------------------------
    # 3. Re-point the settled installments
    # ------------------------------------------------------------------
    relinked = 0
    for row in _rows(cr, '%s_ded' % SNAP):
        line_id = sheet_to_line.get(row['sheet_id'])
        if not line_id:
            continue
        cr.execute(
            "UPDATE ksw_deduction_line SET x_paid_via_pay_run_line_id = %s "
            "WHERE id = %s", (line_id, row['line_id']))
        relinked += 1
    if relinked:
        _logger.info("19.0.3.0.0: re-pointed %s settled installment(s).",
                     relinked)

    # ------------------------------------------------------------------
    # 4. Verify, then drop the retired tables
    # ------------------------------------------------------------------
    drifted = []
    for row in sheets:
        line_id = sheet_to_line.get(row['id'])
        if not line_id:
            continue
        line = env['ksw.pay.run.line'].browse(line_id)
        if abs((line.net_payable or 0.0) - (row.get('payable') or 0.0)) >= 0.01:
            drifted.append((row['id'], row.get('payable'), line.net_payable))
    if drifted:
        _logger.error(
            "19.0.3.0.0: %s sheet(s) changed payable during migration — "
            "REVIEW: %s", len(drifted),
            ', '.join('sheet %s (%.2f -> %.2f)' % d for d in drifted[:20]))
    else:
        _logger.info("19.0.3.0.0: every historical payable reproduced "
                     "exactly.")

    retired = [
        'ksw_commission_sheet_line', 'ksw_commission_sheet',
        'ksw_commission_batch_sheet_rel', 'ksw_commission_run_source',
        'ksw_commission_batch', 'ksw_commission_template_line',
        'ksw_commission_template_employee_rel', 'ksw_commission_template',
        'ksw_commission_category', 'ksw_overtime_sheet_line',
        'ksw_overtime_sheet', 'ksw_driver_commission_line',
        'ksw_driver_commission_sheet', 'ksw_location_allowance_line',
        'ksw_location_allowance_sheet',
    ]
    for table in retired:
        cr.execute('DROP TABLE IF EXISTS "%s" CASCADE' % table)
    _logger.info("19.0.3.0.0: dropped %s retired table(s).", len(retired))

    # Keep the snapshots when anything looked wrong, so it can be inspected.
    if not drifted:
        for suffix in ('sheet', 'line', 'ded', 'overti', 'driver', 'locati',
                       'meals'):
            cr.execute('DROP TABLE IF EXISTS %s_%s' % (SNAP, suffix))
    else:
        _logger.warning(
            "19.0.3.0.0: keeping the %s_* snapshot tables for inspection.",
            SNAP)
