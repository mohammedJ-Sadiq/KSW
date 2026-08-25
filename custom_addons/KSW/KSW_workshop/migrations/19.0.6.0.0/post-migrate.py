"""19.0.6.0.0 — derive request_type for the imported history.

The legacy Google Sheet never had a "request type" column, so all 17,079
imported requests landed with request_type NULL and the single most useful
report ("how many oil changes this year?") returned nothing at all. The
description text does carry the answer — ~44% of all requests mention oil in
one spelling or another — so it is classified here by keyword.

Raw SQL, not an ORM write() loop, for the same reason as the 19.0.2.0.0
migration next door and then some: request_type carries tracking=True, and
ksw.workshop.request's follower is the Workshop Manager, whose partner email
is info@alkawthersw.com. An ORM write across 17k records would post 17k
chatter messages and queue 17k notification emails — exactly the incident
that followed the history import itself (Odoo 19 Pitfalls #93).

Idempotent: only rows where request_type IS NULL are touched, so re-running
the upgrade never reclassifies a value a human has since corrected. Rows that
match no keyword are left NULL on purpose and report as "Unclassified" —
better an honest gap than a wrong bucket.
"""

from odoo.addons.KSW_workshop.models.ksw_workshop_request import REQUEST_TYPE_KEYWORDS


def migrate(cr, version):
    if not version:
        return

    # Applied in order, each guarded by "still NULL", so the first matching
    # pattern wins for the ~400 descriptions that mention more than one kind
    # of work. Ordering therefore encodes priority, not just grouping.
    for request_type, pattern in REQUEST_TYPE_KEYWORDS:
        cr.execute(
            """
            UPDATE ksw_workshop_request
            SET request_type = %s,
                x_request_type_derived = TRUE
            WHERE request_type IS NULL
              AND description IS NOT NULL
              AND description ~* %s
            """,
            (request_type, pattern),
        )
