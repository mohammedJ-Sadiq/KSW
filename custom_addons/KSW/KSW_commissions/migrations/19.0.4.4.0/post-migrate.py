"""Re-run the Friday Work Allowance correction from 19.0.4.2.0.

KSWCO never got it. The manifest bump to 19.0.4.2.0 reached production in a
deploy that did not carry the ``migrations/19.0.4.2.0`` folder with it, so by
the time the script existed on disk the database already recorded 4.2.0 and
Odoo had no version change left to fire it on: the 4.3.0 script ran (Driver
Trips is department-scoped and import-only there, as it should be) while 4.2.0
was silently skipped for good. The supervisor kept being asked for a date per
Friday — the very thing 4.2.0 dropped — with the fixed code and the fixed
``<record>`` both sitting in the container, correct and unread, because the
catalog is ``noupdate="1"`` (CLAUDE.md pitfall #2).

The correction itself is unchanged and idempotent: a database that already
took it (odoo_dev) is left alone, and so is one where somebody has since
ticked Per Occurrence back on deliberately.

Migrations only fire on a version change (pitfall #45), so finishing a job
that was skipped needs a new folder rather than an edit to the old one.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})

    component = env.ref('KSW_commissions.pay_component_friday',
                        raise_if_not_found=False)
    if not component:
        _logger.info('Friday Work Allowance is not installed; nothing to do.')
        return
    if not component.needs_date:
        _logger.info('Friday Work Allowance already records a monthly total.')
        return

    dated = env['ksw.pay.entry'].search_count([
        ('component_id', '=', component.id), ('date', '!=', False)])
    component.needs_date = False
    _logger.info(
        'Friday Work Allowance now records a monthly total '
        '(Per Occurrence off). %s existing entries keep a date that is no '
        'longer asked for.', dated)
