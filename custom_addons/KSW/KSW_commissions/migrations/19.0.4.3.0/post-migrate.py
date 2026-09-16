"""Driver Trips stops being a work-site batch and becomes a department one.

Four things have to move together, or the change ships doing nothing (or
worse, leaves the live draft batch unusable):

1. **The catalog**, twice over. ``pay_component_data.xml`` is
   ``noupdate="1"``, so correcting ``scope`` — or setting the new
   ``entries_import_only`` — in the ``<record>`` reaches fresh installs
   only (pitfall #2). The flip for an existing database is this.
2. **The batches already recorded against a site.** Their component is
   department-scoped from now on, so ``_check_scope`` wants a department
   the moment anyone writes to them — and one of them is a live draft in
   KSWCO. The department is derived from the employees actually on the
   batch, which is the only honest source: it is who was paid.
3. **The required-trips base**, which used to be read off the batch's
   site and now comes from the single seeded settings record. If the site
   these batches used carried a figure other than the seeded 50, it is
   carried across so the next import reproduces the same allowance.

``site_id`` is deliberately left on the historical batches. It is a record
of where those trips were driven; nothing reads it, and the field is
invisible now that no component is site-scoped.
"""
import logging

_logger = logging.getLogger(__name__)

DEFAULT_SITE_XMLID = ('KSW_commissions', 'site_trip_default')


def _trips_component_id(cr):
    cr.execute("SELECT id FROM ksw_pay_component WHERE code = 'TRIPS'")
    row = cr.fetchone()
    return row[0] if row else None


def _default_site_id(cr):
    cr.execute("""
        SELECT res_id FROM ir_model_data
         WHERE module = %s AND name = %s AND model = 'ksw.site'
    """, DEFAULT_SITE_XMLID)
    row = cr.fetchone()
    return row[0] if row else None


def migrate(cr, version):
    if not version:
        return

    component_id = _trips_component_id(cr)
    if not component_id:
        _logger.info('KSW_commissions: no Driver Trips component — nothing '
                     'to re-scope.')
        return

    # 1. The catalog. Both flags, for the same reason: the seed lives in a
    #    noupdate="1" block, so correcting the <record> reaches fresh
    #    installs only (pitfall #2) — and a noupdate record is never
    #    re-asserted afterwards either (pitfall #154).
    cr.execute("""
        UPDATE ksw_pay_component SET scope = 'department'
         WHERE id = %s AND scope = 'site'
    """, (component_id,))
    rescoped = cr.rowcount

    # Driver Trips is imported and reviewed, never typed. Setting it here
    # rather than only in the seed is what makes an existing database
    # actually get the lock.
    cr.execute("""
        UPDATE ksw_pay_component SET entries_import_only = TRUE
         WHERE id = %s AND entries_import_only IS NOT TRUE
    """, (component_id,))
    locked = cr.rowcount

    # 3. Carry the required-trips base across before the sites lose their
    #    meaning. Take it from the site of the most recent trips batch, and
    #    only when it differs from what the record was seeded with.
    default_site_id = _default_site_id(cr)
    carried = None
    if default_site_id:
        # Assert the type rather than trusting the data file: it is in a
        # noupdate="1" block, so it writes nothing to a record that already
        # exists. If the row was ever created or defaulted as a location —
        # a re-added column re-applies the field default to every row — it
        # would sit in the Work Sites list and in every picker, which is
        # precisely what it must never do.
        cr.execute("""
            UPDATE ksw_site SET site_type = 'calculation'
             WHERE id = %s AND site_type IS DISTINCT FROM 'calculation'
        """, (default_site_id,))
        if cr.rowcount:
            _logger.info('KSW_commissions: trip settings record restored to '
                         'site_type=calculation.')
    if default_site_id:
        cr.execute("""
            SELECT s.required_trips_full_month
              FROM ksw_pay_batch b
              JOIN ksw_site s ON s.id = b.site_id
             WHERE b.component_id = %s AND b.site_id IS NOT NULL
             ORDER BY b.period DESC, b.id DESC
             LIMIT 1
        """, (component_id,))
        row = cr.fetchone()
        if row and row[0] is not None:
            cr.execute("""
                UPDATE ksw_site SET required_trips_full_month = %s
                 WHERE id = %s AND required_trips_full_month IS DISTINCT FROM %s
            """, (row[0], default_site_id, row[0]))
            if cr.rowcount:
                carried = row[0]

    # 2. The historical batches. One UPDATE, department taken from the
    #    employees on the batch — the most common one, so a batch holding a
    #    stray employee from elsewhere still lands where it belongs.
    cr.execute("""
        WITH batch_dept AS (
            SELECT b.id AS batch_id, v.department_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY b.id
                       ORDER BY COUNT(*) DESC, v.department_id
                   ) AS rank
              FROM ksw_pay_batch b
              JOIN ksw_pay_entry e ON e.batch_id = b.id
              JOIN hr_employee h ON h.id = e.employee_id
              JOIN hr_version v ON v.id = h.current_version_id
             WHERE b.component_id = %s
               AND b.department_id IS NULL
               AND v.department_id IS NOT NULL
             GROUP BY b.id, v.department_id
        )
        UPDATE ksw_pay_batch b
           SET department_id = bd.department_id
          FROM batch_dept bd
         WHERE b.id = bd.batch_id AND bd.rank = 1
        RETURNING b.id, b.department_id
    """, (component_id,))
    moved = cr.fetchall()

    # Anything left without a department could not be derived — it has no
    # entries, or none of them resolve to a department. Name it: it is a
    # batch somebody will open and find they cannot save.
    cr.execute("""
        SELECT id, name FROM ksw_pay_batch
         WHERE component_id = %s AND department_id IS NULL
    """, (component_id,))
    orphans = cr.fetchall()

    _logger.info(
        'KSW_commissions: Driver Trips re-scoped to department '
        '(component flipped: %s; set import-only: %s). %s historical '
        'batch(es) given a department: %s. Required-trips base carried '
        'across: %s. Batches with no derivable department: %s',
        bool(rescoped), bool(locked), len(moved), moved,
        carried if carried is not None else 'unchanged',
        orphans or 'none',
    )
