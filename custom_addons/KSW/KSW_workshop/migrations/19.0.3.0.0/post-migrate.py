"""19.0.3.0.0 — deterministically restore parts_cost after it becomes a
stored compute.

pre-migrate.py already moved every historical parts_cost value into the new
parts_extra_cost column. By the time this runs, the ORM's install-time
recompute of parts_cost (= part_lines_cost + parts_extra_cost) may or may not
have already fired depending on registry init order — rather than rely on
that, restore the value directly so the result is deterministic regardless.

No historical request has any ksw.workshop.part.line rows yet (the feature
did not exist before this version), so part_lines_cost is unconditionally 0.0
for every existing row.
"""


def migrate(cr, version):
    if not version:
        return

    cr.execute("""
        UPDATE ksw_workshop_request
        SET part_lines_cost = 0.0,
            parts_cost = COALESCE(parts_extra_cost, 0.0)
    """)
