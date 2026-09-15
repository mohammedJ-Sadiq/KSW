"""19.0.11.0.0 — carry request_type into request_type_ids, and re-scan for the rest.

Two passes, deliberately separate:

1. **Carry over.** Every row's existing `request_type` becomes the matching
   tag, 1:1. Nothing is re-judged: whatever each of the 17,079 rows was
   classified as in Aug 2026, it still is.

2. **Re-scan.** The keyword rules are then run again over every description
   and *add* every other type they match. This is what the Many2many exists
   for: 872 of the imported descriptions match more than one rule, and the
   Selection could only keep the first — an oil change that also replaced the
   brake pads was recorded as oil alone. Those rows get the rest of their
   answer back. Decided with the user (2026-09-15), eyes open: it assigns ~950
   classifications no person ever made, which is the trade against a report
   that under-counts brake work by design. `x_request_type_derived` stays TRUE
   on every row it touches, so a report can still separate derived tags from
   chosen ones — and every one of the 12,626 typed rows is already derived
   (there are no human-chosen values in the history to overwrite).

The keywords come from the seeded ksw.workshop.request.type rows, NOT from
REQUEST_TYPE_KEYWORDS: the register is the live ruleset from now on, and if
the manager has already corrected a keyword, the re-scan must use his version.
`data/request_types.xml` is loaded before post-migrate runs, so the rows are
there.

Raw SQL, not an ORM write() loop: `request_type_ids` carries tracking=True and
this model's follower is info@alkawthersw.com — an ORM write across 17k
records would post 17k chatter messages and queue 17k emails, which is exactly
what the history import itself did (Odoo 19 Pitfalls #93).

Idempotent: every INSERT is NOT EXISTS-guarded, so re-running adds nothing.
"""

import logging
import re

_logger = logging.getLogger(__name__)

# Selection key -> the XML id of the type record seeded for it.
SELECTION_TO_XMLID = {
    'oil_filters': 'request_type_oil_filters',
    'tyres': 'request_type_tyres',
    'brakes': 'request_type_brakes',
    'electrical': 'request_type_electrical',
    'bodywork': 'request_type_bodywork',
    'water_pump': 'request_type_water_pump',
    'hoses': 'request_type_hoses',
    'mechanical': 'request_type_mechanical',
    'inspection': 'request_type_inspection',
}


def _seeded_type_ids(cr):
    cr.execute(
        """
        SELECT name, res_id FROM ir_model_data
        WHERE module = 'KSW_workshop' AND model = 'ksw.workshop.request.type'
        """
    )
    return dict(cr.fetchall())


def _keyword_pattern(keywords):
    """Same expression the runtime tagger builds — see
    ksw.workshop.request.type.keyword_pattern(). Kept in step by hand because a
    migration must not import model code that may have moved on."""
    terms = [term.strip() for term in (keywords or '').replace('\n', ',').split(',')]
    return '|'.join(re.escape(term) for term in terms if term)


def migrate(cr, version):
    if not version:
        return

    by_xmlid = _seeded_type_ids(cr)
    if not by_xmlid:
        _logger.warning('No seeded request types found — nothing to backfill.')
        return

    # --- pass 1: the existing value, 1:1 ----------------------------------
    carried = 0
    for selection_key, xmlid in SELECTION_TO_XMLID.items():
        type_id = by_xmlid.get(xmlid)
        if not type_id:
            continue
        cr.execute(
            """
            INSERT INTO ksw_workshop_request_type_rel (request_id, type_id)
            SELECT r.id, %s FROM ksw_workshop_request r
            WHERE r.request_type = %s
              AND NOT EXISTS (
                  SELECT 1 FROM ksw_workshop_request_type_rel rel
                  WHERE rel.request_id = r.id AND rel.type_id = %s)
            """,
            (type_id, selection_key, type_id),
        )
        carried += cr.rowcount

    # --- pass 2: every other match the rules find --------------------------
    cr.execute('SELECT id, keywords FROM ksw_workshop_request_type WHERE active')
    added = 0
    for type_id, keywords in cr.fetchall():
        pattern = _keyword_pattern(keywords)
        if not pattern:
            continue
        cr.execute(
            """
            INSERT INTO ksw_workshop_request_type_rel (request_id, type_id)
            SELECT r.id, %s FROM ksw_workshop_request r
            WHERE r.description IS NOT NULL
              AND r.description ~* %s
              AND NOT EXISTS (
                  SELECT 1 FROM ksw_workshop_request_type_rel rel
                  WHERE rel.request_id = r.id AND rel.type_id = %s)
            """,
            (type_id, pattern, type_id),
        )
        added += cr.rowcount

    # Every tag above came from a rule, so say so — including on the rows that
    # had no type at all before and are now tagged for the first time.
    cr.execute(
        """
        UPDATE ksw_workshop_request r
        SET x_request_type_derived = TRUE
        WHERE r.x_request_type_derived IS DISTINCT FROM TRUE
          AND EXISTS (SELECT 1 FROM ksw_workshop_request_type_rel rel
                      WHERE rel.request_id = r.id)
        """
    )
    flagged = cr.rowcount

    cr.execute('SELECT count(DISTINCT request_id) FROM ksw_workshop_request_type_rel')
    tagged = cr.fetchone()[0]
    cr.execute('SELECT count(*) FROM ksw_workshop_request')
    total = cr.fetchone()[0]
    _logger.info(
        'Request types: %s tags carried over, %s added by re-scan, %s rows flagged '
        'derived. %s of %s requests now carry at least one tag.',
        carried, added, flagged, tagged, total,
    )
