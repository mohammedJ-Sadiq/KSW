"""Public holidays, tagged with the pay occasion they are.

A holiday bonus is earned on one known day. HR already dates those days in
Time Off > Public Holidays; the tag says which of them a component pays for,
so ``ksw.pay.entry._paid_day_date`` can read the day instead of a supervisor
typing it (or nobody typing it, and the vacation hold flagging every
returnee for the whole month).
"""
from odoo import api, fields, models

from .ksw_pay_component import HOLIDAY_OCCASIONS

#: Words in a holiday's name that say which occasion it is, English and
#: Arabic. Only a first guess: the field stays editable.
_OCCASION_WORDS = {
    'national_day': ('national', 'وطني'),
    'foundation_day': ('foundation', 'تأسيس'),
    'eid_fitr': ('fitr', 'فطر'),
    'eid_adha': ('adha', 'أضحى', 'اضحى'),
}


def guess_occasion(name):
    name = (name or '').lower()
    for occasion, words in _OCCASION_WORDS.items():
        if any(word in name for word in words):
            return occasion
    return False


class ResourceCalendarLeaves(models.Model):
    _inherit = 'resource.calendar.leaves'

    x_pay_occasion = fields.Selection(
        HOLIDAY_OCCASIONS, string='Pay Occasion',
        compute='_compute_pay_occasion', store=True, readonly=False,
        help='Which holiday bonus this day pays. Filled from the name '
             '(National Day, Foundation Day, Eid al-Fitr, Eid al-Adha); '
             'correct it if the guess is wrong. A bonus line for that '
             'occasion takes its date from here.',
    )

    @api.depends('name', 'resource_id')
    def _compute_pay_occasion(self):
        for rec in self:
            # Only a company-wide holiday is an occasion; one person's
            # time off is not, whatever it is called.
            rec.x_pay_occasion = (
                guess_occasion(rec.name) if not rec.resource_id else False)
