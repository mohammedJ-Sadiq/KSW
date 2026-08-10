"""19.0.2.0.0 — snapshot the scalar contributions before the ORM loads.

``post-migrate.py`` rebuilds the five per-type scalar columns as tagged
contribution lines. It must read those columns *as they were*, but by the time
a post-migrate runs the ORM has already loaded the new model definitions, in
which the same five fields are computed from the very lines we are about to
create — so anything that triggers a recompute in between would zero them and
the amounts would be lost silently.

Snapshotting here, in raw SQL before any of that happens, removes the ordering
question entirely.

Per CLAUDE.md gotcha #49 a pre-migrate cannot assume its own columns exist:
everything is guarded on ``information_schema`` so a partially-upgraded or
re-run database does not crash.
"""
import logging

_logger = logging.getLogger(__name__)

SNAPSHOT_TABLE = 'ksw_commission_sheet_scalar_snapshot_19_2_0_0'

COLUMNS = [
    'driver_commission_amount',
    'location_allowance_amount',
    'sales_commission_amount',
    'collection_commission_amount',
    'combined_commission_amount',
    'total',
]


def _existing_columns(cr, table, columns):
    cr.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = %s AND column_name IN %s",
        (table, tuple(columns)),
    )
    return {row[0] for row in cr.fetchall()}


def _reshard_location_allowance(cr):
    """Give the location-allowance sheets a department before it is required.

    The model went from one company-wide sheet per month
    (``UNIQUE(period)``) to one per (department, period), because a
    company-wide singleton cannot be scoped to a supervisor. The new
    ``department_id`` is required, so existing rows need a value before the
    ORM adds the NOT NULL, and the old unique index has to go or it would
    still forbid a second department in the same month.

    Empty sheets are simply removed — nothing is lost and the supervisors
    create their own. A sheet that *does* have lines is assigned the
    department of its first line's employee and logged, so the remainder
    can be split by hand rather than silently mangled here.
    """
    cr.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
        ('ksw_location_allowance_sheet',),
    )
    if not cr.fetchone():
        return

    cr.execute("""
        DELETE FROM ksw_location_allowance_sheet s
         WHERE NOT EXISTS (SELECT 1 FROM ksw_location_allowance_line l
                            WHERE l.sheet_id = s.id)
     RETURNING s.id
    """)
    removed = cr.rowcount
    if removed:
        _logger.info(
            "KSW_commissions 19.0.2.0.0: removed %s empty location-allowance "
            "sheet(s) ahead of the per-department reshard.", removed)

    cr.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        ('ksw_location_allowance_sheet', 'department_id'),
    )
    if not cr.fetchone():
        cr.execute("ALTER TABLE ksw_location_allowance_sheet "
                   "ADD COLUMN department_id integer")

    # First line's employee decides the department.
    cr.execute("""
        UPDATE ksw_location_allowance_sheet s
           SET department_id = sub.department_id
          FROM (SELECT DISTINCT ON (l.sheet_id) l.sheet_id, l.department_id
                  FROM ksw_location_allowance_line l
                 WHERE l.department_id IS NOT NULL
              ORDER BY l.sheet_id, l.id) sub
         WHERE s.id = sub.sheet_id AND s.department_id IS NULL
    """)

    cr.execute("SELECT id FROM ksw_location_allowance_sheet "
               "WHERE department_id IS NULL")
    orphans = [r[0] for r in cr.fetchall()]
    if orphans:
        _logger.warning(
            "KSW_commissions 19.0.2.0.0: location-allowance sheet(s) %s have "
            "lines but no resolvable department; deleting them so the "
            "reshard can proceed — re-enter those meals per department.",
            orphans)
        cr.execute("DELETE FROM ksw_location_allowance_line "
                   "WHERE sheet_id IN %s", (tuple(orphans),))
        cr.execute("DELETE FROM ksw_location_allowance_sheet "
                   "WHERE id IN %s", (tuple(orphans),))

    # The old company-wide unique index would block the new shape.
    cr.execute("ALTER TABLE ksw_location_allowance_sheet "
               "DROP CONSTRAINT IF EXISTS ksw_location_allowance_sheet_unique_period")


def migrate(cr, version):
    if not version:
        return

    _reshard_location_allowance(cr)

    cr.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
        ('ksw_commission_sheet',),
    )
    if not cr.fetchone():
        return

    present = _existing_columns(cr, 'ksw_commission_sheet', COLUMNS)
    missing = [c for c in COLUMNS if c not in present]
    if missing:
        _logger.warning(
            "KSW_commissions 19.0.2.0.0 pre-migrate: columns %s are absent; "
            "nothing to snapshot.", ', '.join(missing))
        return

    cr.execute("DROP TABLE IF EXISTS %s" % SNAPSHOT_TABLE)
    cr.execute(
        "CREATE TABLE {table} AS SELECT id, {cols} "
        "FROM ksw_commission_sheet WHERE {where}".format(
            table=SNAPSHOT_TABLE,
            cols=', '.join(COLUMNS),
            where=' OR '.join(
                '%s != 0' % c for c in COLUMNS if c != 'total'),
        )
    )
    cr.execute("SELECT count(*) FROM %s" % SNAPSHOT_TABLE)
    _logger.info(
        "KSW_commissions 19.0.2.0.0 pre-migrate: snapshotted %s sheet(s) "
        "carrying scalar contributions.", cr.fetchone()[0])
