"""Accounting checks commissions before the General Manager (Nov 2026).

Two things an upgrade has to do for a database that already has handovers:

* Rows already in front of the GM stay with him. They were handed over
  before Accounting had a step, so they are stamped as checked (by OdooBot,
  now) rather than pulled back to Accounting in the middle of his review.
* Home ▸ Waiting for you: the commission counter becomes "waiting for me"
  (Accounting's check or the GM's decision), and sub-batches get one too.
  KSW_backend_theme's sources file is noupdate, so the existing record is
  written here, and the new one is created under that module's xml id so a
  later upgrade of the theme does not add it a second time.

end-migrate, not post-migrate: ksw.home.inbox.source belongs to
KSW_backend_theme, which loads after this module (Odoo 19 Pitfalls #45).
"""
import logging

from odoo import SUPERUSER_ID, api, fields

_logger = logging.getLogger(__name__)

SOURCES = {
    'inbox_commission_submissions': {
        'name': 'Commission submissions to check or approve',
        'sequence': 40,
        'model_name': 'ksw.pay.submission',
        'domain': "[('x_is_pending_my_action', '=', True)]",
        'icon': 'fa-check-square-o',
        'name_ar': 'عمولات أقسام بانتظار تدقيقي أو اعتمادي',
    },
    'inbox_commission_sub_batches': {
        'name': 'Commission sub-batches to check or approve',
        'sequence': 41,
        'model_name': 'ksw.pay.sub.batch',
        'domain': "[('x_is_pending_my_action', '=', True)]",
        'icon': 'fa-scissors',
        'name_ar': 'دفعات عمولات فرعية بانتظار تدقيقي أو اعتمادي',
    },
}


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Entry = env['ksw.pay.entry']
    waiting = Entry.search([('state', '=', 'submitted'),
                            ('x_acc_checked_by', '=', False),
                            ('x_vacation_payslip_id', '=', False)])._handed_over()
    waiting.write({'x_acc_checked_by': env.ref('base.user_root').id,
                   'x_acc_checked_date': fields.Datetime.now()})
    _logger.info('Accounting check: %s row(s) already with the GM stamped '
                 'as checked: %s', len(waiting), waiting.ids)

    if 'ksw.home.inbox.source' not in env:
        _logger.info('KSW_backend_theme not installed: no Home counters.')
        return
    Source = env['ksw.home.inbox.source'].with_context(active_test=False)
    Data = env['ir.model.data']
    arabic = env['res.lang']._lang_get('ar_001')
    for name, vals in SOURCES.items():
        vals = dict(vals)
        name_ar = vals.pop('name_ar')
        record = env.ref('KSW_backend_theme.%s' % name,
                         raise_if_not_found=False)
        if record:
            record.write({'name': vals['name'], 'domain': vals['domain']})
        else:
            record = Source.create(vals)
            Data.create({'module': 'KSW_backend_theme', 'name': name,
                         'model': record._name, 'res_id': record.id,
                         'noupdate': True})
            _logger.info('Home counter %s created (id %s).', name,
                         record.id)
        # The old Arabic ("awaiting my approval") would otherwise stay.
        if arabic:
            record.with_context(lang='ar_001').write({'name': name_ar})
