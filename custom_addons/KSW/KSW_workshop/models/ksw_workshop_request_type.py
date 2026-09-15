import re

from odoo import _, api, fields, models


class KswWorkshopRequestType(models.Model):
    """What kind of work a request is about — a tag, and the keywords that earn it.

    The keywords live here, in the database, rather than in a Python constant:
    classifying the long tail is permanent work that belongs to the workshop
    manager, and "add ز بت as another spelling of زيت" should not be a code
    change, a deploy and an upgrade. Same reasoning as a commission pay type
    being configuration rather than code.

    The nine rows seeded with the module are exactly the rules that classified
    the 17,079 imported requests (REQUEST_TYPE_KEYWORDS in
    ksw_workshop_request.py, which stays as the seed's source and as the
    19.0.6.0.0 migration's input). They are seeded `noupdate="1"` so that an
    upgrade never overwrites a keyword the manager has since added.
    """
    _name = 'ksw.workshop.request.type'
    _description = 'Workshop Request Type'
    _order = 'sequence, id'

    # No unique constraint on name: it is translated, so the column is jsonb
    # and `unique(name)` would compare whole JSON documents rather than the
    # label anyone actually reads.
    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    color = fields.Integer()
    keywords = fields.Text(
        help="Words that earn this tag when they appear in a request's description, "
             "separated by commas. Matching ignores case and matches inside longer "
             "words, so 'فلتر' also catches 'الفلتر'. Add the misspellings people "
             "actually type — 'زبت' and 'chance oil' are hundreds of real requests.")
    active = fields.Boolean(default=True)

    request_ids = fields.Many2many(
        'ksw.workshop.request', 'ksw_workshop_request_type_rel', 'type_id', 'request_id',
        string='Requests')
    # Unstored: a display figure on the configuration list. Anything that needs
    # grouping or measuring goes through the Workshop Analysis report, which
    # pivots the requests themselves.
    request_count = fields.Integer(string='Requests', compute='_compute_request_count')

    def _compute_request_count(self):
        counted = {
            rtype.id: count
            for rtype, count in self.env['ksw.workshop.request'].sudo()._read_group(
                [('request_type_ids', 'in', self.ids)],
                ['request_type_ids'], ['__count'],
            )
        }
        for rtype in self:
            rtype.request_count = counted.get(rtype.id, 0)

    def keyword_pattern(self):
        """This type's keywords as one case-insensitive alternation.

        One expression used by both callers — the runtime tagger in
        ksw.workshop.request and the history backfill's SQL `~*` — so the
        keywords cannot mean one thing when a request is saved and another
        when the history is reclassified. Each term is escaped: a manager
        typing "brake (front)" is entering words, not a regular expression.
        """
        self.ensure_one()
        terms = [
            term.strip()
            for term in (self.keywords or '').replace('\n', ',').split(',')
        ]
        return '|'.join(re.escape(term) for term in terms if term)

    @api.model
    def _match_description(self, text):
        """Every type whose keywords appear in `text`.

        All of them, not the first — a job that changed the oil and the brake
        pads is both, and 872 of the 17,079 imported descriptions say so. The
        Selection field this replaced had to throw the rest away.
        """
        if not text:
            return self.browse()
        matched = self.browse()
        # sudo: an ordinary employee submitting a request must be tagged too,
        # and the rules are configuration, not their data.
        for rtype in self.sudo().search([]):
            pattern = rtype.keyword_pattern()
            if pattern and re.search(pattern, text, re.IGNORECASE):
                matched |= rtype
        return matched

    # ------------------------------------------------------------------
    # Re-scanning existing requests
    # ------------------------------------------------------------------
    # The rules get better over time — a new type is added, a spelling nobody
    # had seen turns up. Without this, every improvement only ever reaches
    # requests created after it, and the history keeps the answer the rules
    # gave on the day it was imported.
    def _rescan_requests(self):
        """Re-apply these types' keywords across the whole request history.

        Raw SQL on the relation table, for two reasons. `request_type_ids` is
        `tracking=True` and this model's follower is info@alkawthersw.com, so
        an ORM write across 17k rows would post 17k chatter messages and queue
        17k emails (Odoo 19 Pitfalls #93). And it has to be fast enough to sit
        behind a button.

        **Only requests whose tags are still machine-derived are touched.**
        `x_request_type_derived = False` means a person has said what this
        request is, and no rule change may overrule that — the same guarantee
        the save-time tagger gives, applied retroactively.

        Both directions: a keyword that now matches adds the tag, and one that
        no longer matches removes it. A re-scan is "what would the rules say
        today", not "add more tags" — otherwise fixing a typo'd keyword would
        leave its wrong tags behind for ever.
        """
        # Raw SQL reads the database, and the ORM may still be holding writes
        # that have not reached it — a request whose tags were just corrected
        # in this transaction would still look machine-derived, and the rescan
        # would overrule the correction it is required to respect. Same reason
        # hr.leave._recheck_weekend_grants() flushes before its pass reads a
        # stored compute back out of the DB (Odoo 19 Pitfalls #36).
        self.env.flush_all()
        types = self or self.search([])
        added = removed = 0
        for rtype in types:
            pattern = rtype.keyword_pattern()
            if pattern:
                self.env.cr.execute(
                    """
                    INSERT INTO ksw_workshop_request_type_rel (request_id, type_id)
                    SELECT r.id, %s FROM ksw_workshop_request r
                    WHERE COALESCE(r.x_request_type_derived, TRUE)
                      AND r.description IS NOT NULL
                      AND r.description ~* %s
                      AND NOT EXISTS (
                          SELECT 1 FROM ksw_workshop_request_type_rel rel
                          WHERE rel.request_id = r.id AND rel.type_id = %s)
                    """,
                    (rtype.id, pattern, rtype.id),
                )
                added += self.env.cr.rowcount
            self.env.cr.execute(
                """
                DELETE FROM ksw_workshop_request_type_rel rel
                USING ksw_workshop_request r
                WHERE rel.request_id = r.id
                  AND rel.type_id = %s
                  AND COALESCE(r.x_request_type_derived, TRUE)
                  AND (%s = '' OR r.description IS NULL OR NOT r.description ~* %s)
                """,
                (rtype.id, pattern, pattern or '(?!)'),
            )
            removed += self.env.cr.rowcount
        # The ORM has no idea any of that happened.
        self.env['ksw.workshop.request'].invalidate_model(['request_type_ids'])
        self.invalidate_model(['request_ids'])
        return added, removed

    def action_rescan_requests(self):
        """Button: apply these types to the requests already in the system."""
        added, removed = self._rescan_requests()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': _('Re-scan complete'),
                'message': _(
                    '%(added)s tags added, %(removed)s removed. Requests whose type '
                    'was set by hand were left alone.',
                    added=added, removed=removed,
                ),
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

    @api.model
    def action_rescan_all(self):
        return self.search([]).action_rescan_requests()
