# -*- coding: utf-8 -*-
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError

# Words that say nothing about what is wrong: vehicle nouns, "problem",
# "please", verbs of requesting. They dominate any frequency count and would
# bury the words that matter. Extend by ignoring a word in the report itself —
# that is what ksw.workshop.keyword.ignored is for, so this list never needs
# a developer again.
NOISE_WORDS = {
    'في', 'من', 'على', 'مع', 'الى', 'إلى', 'عن', 'هذا', 'هذه', 'ان', 'أن', 'او', 'أو',
    'رقم', 'يوجد', 'يرجى', 'ارجو', 'المطلوب', 'طلب', 'تم', 'لا', 'يتم', 'عدد',
    'تريلا', 'خزان', 'سياره', 'سيارة', 'السياره', 'السيارة', 'شاحنة', 'ايسوزو', 'إيسوزو',
    'مشكله', 'مشكلة', 'مشاكل', 'تحتاج', 'يحتاج', 'تبديل', 'تغيير', 'اصلاح', 'إصلاح',
    'تركيب', 'صيانة', 'صيانه', 'فحص', 'تشييك', 'التشيك', 'الموافقة', 'ورشة', 'السائق',
    'الشركة', 'وقف', 'متوقفه', 'متوقف', 'خربان', 'خربانه', 'كسر', 'مكسور', 'جديد',
}

MIN_WORD_LENGTH = 3
SUGGESTION_LIMIT = 60


class KswWorkshopKeywordIgnored(models.Model):
    """A word the workshop manager has decided is not worth a keyword.

    Without this the suggestion list shows تريلا and مشكله at the top for
    ever and has to be mentally skipped every month. Ignoring is a decision,
    so it is recorded rather than hard-coded.
    """
    _name = 'ksw.workshop.keyword.ignored'
    _description = 'Ignored Keyword Suggestion'
    _order = 'name'

    name = fields.Char(required=True)

    _name_uniq = models.Constraint('unique(name)', 'This word is already ignored.')


class KswWorkshopKeywordSuggestion(models.TransientModel):
    """What the unclassified pile is trying to tell you.

    The keyword rules only improve when somebody notices a wording they do not
    cover, and nothing about an untagged request announces itself. This counts
    the words in requests that matched no type at all and ranks them by how
    many requests use them — so "what new wording appeared?" stops being a
    question anyone has to answer from memory, and becomes a list with the
    biggest gap at the top.

    Transient on purpose: it is a view of the corpus as it stands, not a
    record of anything. Filing a word into a type is the durable act.
    """
    _name = 'ksw.workshop.keyword.suggestion'
    _description = 'Suggested Keyword'
    _order = 'request_count desc, name'

    name = fields.Char(string='Word', readonly=True)
    request_count = fields.Integer(string='Unclassified Requests', readonly=True)
    sample = fields.Char(string='Example', readonly=True)
    type_id = fields.Many2one(
        'ksw.workshop.request.type', string='File Under',
        help="Pick the type this word belongs to, then use Add to Type.")

    # ------------------------------------------------------------------
    @api.model
    def _collect(self):
        """Word -> (how many untagged requests use it, one example)."""
        self.env.cr.execute(
            """
            SELECT r.id, r.description FROM ksw_workshop_request r
            WHERE r.description IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM ksw_workshop_request_type_rel rel
                              WHERE rel.request_id = r.id)
            """
        )
        rows = self.env.cr.fetchall()
        ignored = set(self.env['ksw.workshop.keyword.ignored'].search([]).mapped('name'))

        counts, samples = {}, {}
        for _request_id, description in rows:
            # Counted once per request, not once per mention: the question is
            # "how many jobs would this keyword catch", not "how often is it
            # typed" — a description repeating a word is still one request.
            for word in set(re.split(r'[^\w؀-ۿ]+', description)):
                word = word.strip()
                if (len(word) < MIN_WORD_LENGTH or word.isdigit()
                        or word in NOISE_WORDS or word in ignored):
                    continue
                counts[word] = counts.get(word, 0) + 1
                samples.setdefault(word, ' '.join(description.split())[:120])
        return counts, samples

    @api.model
    def action_open_suggestions(self):
        self.search([]).unlink()          # transient, but the same session may re-open
        counts, samples = self._collect()
        top = sorted(counts.items(), key=lambda item: -item[1])[:SUGGESTION_LIMIT]
        self.create([
            {'name': word, 'request_count': count, 'sample': samples.get(word)}
            for word, count in top
        ])
        return {
            'type': 'ir.actions.act_window',
            'name': _('Suggested Keywords'),
            'res_model': 'ksw.workshop.keyword.suggestion',
            'view_mode': 'list',
            'target': 'current',
            'context': {'create': False},
            'help': _(
                '<p class="o_view_nocontent_smiling_face">Nothing to suggest</p>'
                '<p>Every request in the system already matched at least one type.</p>'
            ),
        }

    # ------------------------------------------------------------------
    def action_add_to_type(self):
        """File this word into the chosen type, and apply it to history."""
        for suggestion in self:
            if not suggestion.type_id:
                raise UserError(_(
                    'Pick a type in "File Under" first — that is where %s will be added.',
                    suggestion.name,
                ))
            rtype = suggestion.type_id
            existing = [
                term.strip()
                for term in (rtype.keywords or '').replace('\n', ',').split(',')
                if term.strip()
            ]
            if suggestion.name not in existing:
                rtype.keywords = ', '.join(existing + [suggestion.name])
            # Apply it where it already applies — the whole point of adding a
            # keyword is usually the requests that are already waiting for it.
            rtype._rescan_requests()
        self.unlink()
        return self.action_open_suggestions()

    def action_ignore(self):
        Ignored = self.env['ksw.workshop.keyword.ignored']
        for suggestion in self:
            if not Ignored.search([('name', '=', suggestion.name)], limit=1):
                Ignored.create({'name': suggestion.name})
        self.unlink()
        return self.action_open_suggestions()
