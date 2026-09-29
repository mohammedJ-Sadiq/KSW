# -*- coding: utf-8 -*-
"""The Explanation panel must never execute what a supervisor typed.

Audit 2026-09-28: `details` (free text) was string-formatted into the
sanitize=False `explanation` field, so `<img src=x onerror=...>` typed by a
supervisor ran as script in the GM's or accountant's browser.
"""
from .test_bas_trips_import import BasTripsImportCommon

PAYLOAD = '<img src=x onerror="alert(1)">'


class TestExplanationEscaping(BasTripsImportCommon):

    def test_details_are_escaped(self):
        batch = self._batch()
        self._import(batch)
        entry = batch.entry_ids[:1]
        entry.sudo().write({'details': PAYLOAD})
        html = str(entry.explanation or '')
        self.assertNotIn('<img', html)
        self.assertIn('&lt;img', html)
        # the table itself is still real markup
        self.assertIn('<table', html)
