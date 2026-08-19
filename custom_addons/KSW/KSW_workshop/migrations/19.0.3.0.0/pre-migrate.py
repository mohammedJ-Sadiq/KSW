"""19.0.3.0.0 — preserve historical parts_cost before it becomes a stored compute.

parts_cost changes from a plain writable Float to
compute='_compute_parts_cost', store=True (= part_lines_cost +
parts_extra_cost). Both of those are zero for every historical row (no part
lines existed before this version), so the install-time recompute would zero
out all 17,000+ imported spare-parts costs. Move the existing value into the
new parts_extra_cost column instead — semantically correct, since every
historical parts_cost describes a free-text repairs_parts entry, exactly what
parts_extra_cost is for.

The column is added here by hand, in pre-migrate, rather than left for
_auto_init: this script runs BEFORE the ORM adds new columns, and _auto_init
would otherwise backfill the field's Python default (0.0) over the value
written here (same _auto_init-races-the-backfill trap as KSW_fleet/
KSW_workshop's own 19.0.2.0.0 migrations — see Odoo 19 Pitfalls #83).
ADD COLUMN IF NOT EXISTS makes the later _auto_init pass a no-op.

Raw SQL, not an ORM loop: 17,000+ rows, and the ORM path would also trip the
_REPORT_FIELDS write guard on ksw.workshop.request.
"""


def migrate(cr, version):
    if not version:
        return

    cr.execute("""
        ALTER TABLE ksw_workshop_request
        ADD COLUMN IF NOT EXISTS parts_extra_cost double precision
    """)
    cr.execute("""
        UPDATE ksw_workshop_request
        SET parts_extra_cost = COALESCE(parts_cost, 0.0)
        WHERE parts_extra_cost IS NULL
    """)
