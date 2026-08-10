"""19.0.3.0.0 — snapshot the old commission world before the ORM loads.

The module drops fourteen models in favour of five. This runs first, in raw
SQL, and copies everything the post-migrate needs into snapshot tables — for
the same reason as 19.0.2.0.0: by the time a post-migrate runs, the ORM has
already loaded model definitions in which most of these fields no longer
exist, and several of the source tables are about to be dropped.

Nothing is deleted here. The post-migrate rebuilds from the snapshots and only
then removes the retired tables.
"""
import logging

_logger = logging.getLogger(__name__)

SNAP = 'ksw_pay_migration_snapshot_19_3_0_0'


def _table_exists(cr, table):
    cr.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
        (table,))
    return bool(cr.fetchone())


def _column_exists(cr, table, column):
    cr.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s", (table, column))
    return bool(cr.fetchone())


def migrate(cr, version):
    if not version:
        return

    # ---- the per-employee commission sheets -> payment register --------
    if _table_exists(cr, 'ksw_commission_sheet'):
        cr.execute("DROP TABLE IF EXISTS %s_sheet" % SNAP)
        cr.execute("""
            CREATE TABLE {snap}_sheet AS
            SELECT id, employee_id, period, state,
                   COALESCE(total, 0)               AS total,
                   COALESCE(x_loans_amount_locked, 0) AS loans,
                   COALESCE(total_payable, 0)        AS payable,
                   x_unwind_data
              FROM ksw_commission_sheet
        """.format(snap=SNAP))
        cr.execute("SELECT count(*) FROM %s_sheet" % SNAP)
        _logger.info("19.0.3.0.0: snapshotted %s commission sheet(s).",
                     cr.fetchone()[0])

    # ---- their manual lines -> pay entries -----------------------------
    if _table_exists(cr, 'ksw_commission_sheet_line'):
        cr.execute("DROP TABLE IF EXISTS %s_line" % SNAP)
        is_auto = _column_exists(cr, 'ksw_commission_sheet_line', 'is_auto')
        cr.execute("""
            CREATE TABLE {snap}_line AS
            SELECT l.id, l.sheet_id, s.employee_id, s.period,
                   l.category_id, c.code AS category_code, c.name AS category_name,
                   COALESCE(l.quantity, 0) AS quantity,
                   COALESCE(l.amount, 0)   AS amount,
                   l.description,
                   {is_auto} AS is_auto
              FROM ksw_commission_sheet_line l
              JOIN ksw_commission_sheet s ON s.id = l.sheet_id
         LEFT JOIN ksw_commission_category c ON c.id = l.category_id
        """.format(snap=SNAP,
                   is_auto='COALESCE(l.is_auto, false)' if is_auto else 'false'))
        cr.execute("SELECT count(*) FROM %s_line" % SNAP)
        _logger.info("19.0.3.0.0: snapshotted %s commission line(s).",
                     cr.fetchone()[0])

    # ---- the deduction settlement link ---------------------------------
    # x_paid_via_commission_sheet_id becomes x_paid_via_pay_run_line_id; the
    # old column is about to be dropped by the ORM, so keep the mapping.
    if _column_exists(cr, 'ksw_deduction_line',
                      'x_paid_via_commission_sheet_id'):
        cr.execute("DROP TABLE IF EXISTS %s_ded" % SNAP)
        cr.execute("""
            CREATE TABLE {snap}_ded AS
            SELECT id AS line_id, x_paid_via_commission_sheet_id AS sheet_id
              FROM ksw_deduction_line
             WHERE x_paid_via_commission_sheet_id IS NOT NULL
        """.format(snap=SNAP))
        cr.execute("SELECT count(*) FROM %s_ded" % SNAP)
        _logger.info(
            "19.0.3.0.0: snapshotted %s settled installment link(s).",
            cr.fetchone()[0])

    # ---- entry sheets -> pay entries -----------------------------------
    for table, extra in (
        ('ksw_overtime_sheet_line',
         "l.employee_id, s.period, l.date AS entry_date, l.hours AS quantity, "
         "0 AS quantity_ref, l.amount, l.reason, l.details, l.location_id, "
         "'OT' AS component_code, s.department_id, NULL::integer AS site_id"),
        ('ksw_location_allowance_line',
         "l.employee_id, s.period, NULL::date AS entry_date, 0 AS quantity, "
         "0 AS quantity_ref, 0 AS amount, NULL AS reason, NULL AS details, "
         "NULL::integer AS location_id, 'MEALS' AS component_code, "
         "l.department_id, NULL::integer AS site_id"),
        ('ksw_driver_commission_line',
         "l.employee_id, s.period, NULL::date AS entry_date, "
         "l.multiplied_trips AS quantity, l.actual_trips AS quantity_ref, "
         "l.total_commission AS amount, NULL AS reason, NULL AS details, "
         "NULL::integer AS location_id, 'TRIPS' AS component_code, "
         "NULL::integer AS department_id, s.site_id"),
    ):
        sheet_table = table.replace('_line', '_sheet').replace(
            'ksw_driver_commission_sheet', 'ksw_driver_commission_sheet')
        if table == 'ksw_driver_commission_line':
            sheet_table = 'ksw_driver_commission_sheet'
        elif table == 'ksw_overtime_sheet_line':
            sheet_table = 'ksw_overtime_sheet'
        else:
            sheet_table = 'ksw_location_allowance_sheet'
        if not (_table_exists(cr, table) and _table_exists(cr, sheet_table)):
            continue
        snap_name = '%s_%s' % (SNAP, table.split('_')[1][:6])
        cr.execute("DROP TABLE IF EXISTS %s" % snap_name)
        cr.execute("""
            CREATE TABLE {name} AS
            SELECT {cols}, s.state AS sheet_state
              FROM {line} l JOIN {sheet} s ON s.id = l.sheet_id
        """.format(name=snap_name, cols=extra, line=table, sheet=sheet_table))
        cr.execute("SELECT count(*) FROM %s" % snap_name)
        _logger.info("19.0.3.0.0: snapshotted %s row(s) from %s.",
                     cr.fetchone()[0], table)

    # ---- meal quantities need their own shape (3 components) -----------
    if _table_exists(cr, 'ksw_location_allowance_line'):
        cr.execute("DROP TABLE IF EXISTS %s_meals" % SNAP)
        cr.execute("""
            CREATE TABLE {snap}_meals AS
            SELECT l.employee_id, s.period, s.state AS sheet_state,
                   l.department_id,
                   COALESCE(l.breakfast_qty, 0) AS breakfast_qty,
                   COALESCE(l.lunch_qty, 0)     AS lunch_qty,
                   COALESCE(l.dinner_qty, 0)    AS dinner_qty
              FROM ksw_location_allowance_line l
              JOIN ksw_location_allowance_sheet s ON s.id = l.sheet_id
        """.format(snap=SNAP))
