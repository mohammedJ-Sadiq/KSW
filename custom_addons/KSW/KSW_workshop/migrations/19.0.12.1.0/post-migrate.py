# -*- coding: utf-8 -*-
"""19.0.12.0.0 — decompose the Mechanical catch-all, then re-scan everything.

The seventeen new types arrive by themselves: `data/request_types.xml` is
`noupdate="1"`, but noupdate only blocks *updates* to existing records — new
xml ids are still created on upgrade.

Changing the nine ORIGINAL types' keywords is the part that needs this script.
A `<record>` override inside another module's (or its own) `noupdate` block is
silently ignored on every upgrade after the first install (Odoo 19 Pitfalls
#2), so the edits are made through the ORM here.

**What changes and why.** `Mechanical` was carrying six unrelated systems at
once — كلتش (clutch), جير (gearbox), عفشة (suspension), رديتر (radiator),
مكيف (A/C), دنجل (axle). That is exactly what a maintenance taxonomy is meant
to prevent: VMRS gives each of those its own system code (023, 026, 016, 042,
001, 022), and so do SAP PM's object-part catalog and Maximo's failure class.
Those keywords move to the new types that now exist for them, and Mechanical
keeps only genuinely generic wording.

**A keyword the manager has already edited is never overwritten**: each type
is only rewritten if its keywords are still byte-for-byte what was seeded.
The register belongs to him now; this is the module correcting its own seed,
not the module reclaiming the field.

The re-scan at the end is the same `_rescan_requests()` the button calls — one
implementation, so an upgrade and a click cannot classify the corpus
differently. It touches only requests whose tags are still machine-derived.
"""

import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

# xml id -> (keywords exactly as seeded, keywords they should now be).
# The first element is the guard: if it no longer matches, someone has edited
# this type by hand and it is left alone.
REKEY = {
    'request_type_water_pump': (
        'موتور, طرمبة, مضخة',
        # ماتور is how it is spelled in 189 untagged requests — the same word,
        # the other spelling, exactly like زبت/زيت and قير/جير.
        'موتور, ماتور, طرمبة, طرمبه, مضخة',
    ),
    'request_type_mechanical': (
        'كلتش, جير, دبرياج, عفشة, رديتر, مكيف, دنجل',
        # Everything specific has moved to its own VMRS-aligned type; what is
        # left is the word for "mechanical work" with nothing more precise said.
        'ميكانيك, ميكانيكي, ميكانيكا',
    ),
}


def migrate(cr, version):
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})
    RequestType = env['ksw.workshop.request.type']

    # x_request_type_derived was added with default=True, and a default only
    # applies to rows created after it — so the 4,454 requests that matched no
    # keyword were left NULL by the 19.0.11.0.0 backfill (which only stamped
    # TRUE where a tag existed). NULL is not FALSE, but it behaves like it in
    # both places that matter: `WHERE bool_col` drops NULL rows in SQL, and
    # Odoo reads NULL as False in Python. So those rows were invisible to the
    # re-scan AND would never have been re-tagged when their description was
    # edited — the two mechanisms that exist to improve them, both skipping
    # exactly the requests that need improving. A request nobody has tagged by
    # hand is derived by definition.
    cr.execute(
        'UPDATE ksw_workshop_request SET x_request_type_derived = TRUE '
        'WHERE x_request_type_derived IS NULL'
    )
    _logger.info('%s requests had a NULL derived flag, normalised to TRUE.', cr.rowcount)

    rekeyed, skipped = 0, []
    for xmlid, (seeded, corrected) in REKEY.items():
        rtype = env.ref(f'KSW_workshop.{xmlid}', raise_if_not_found=False)
        if not rtype:
            continue
        if (rtype.keywords or '').strip() != seeded:
            skipped.append(rtype.name)
            continue
        rtype.keywords = corrected
        rekeyed += 1

    added, removed = RequestType.search([])._rescan_requests()

    cr.execute("""
        SELECT count(*) FROM ksw_workshop_request r
        WHERE r.description IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM ksw_workshop_request_type_rel rel
                          WHERE rel.request_id = r.id)
    """)
    still_untagged = cr.fetchone()[0]
    cr.execute('SELECT count(*) FROM ksw_workshop_request WHERE description IS NOT NULL')
    total = cr.fetchone()[0]

    _logger.info(
        'Request types: %s types re-keyed (%s left alone as hand-edited), '
        're-scan added %s tags and removed %s. %s of %s requests still '
        'unclassified (%.1f%% coverage).',
        rekeyed, skipped or 'none', added, removed, still_untagged, total,
        (total - still_untagged) / total * 100 if total else 0,
    )
