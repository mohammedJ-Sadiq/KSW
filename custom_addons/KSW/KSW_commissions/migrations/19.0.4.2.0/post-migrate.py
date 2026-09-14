"""19.0.4.2.0 — Friday Work Allowance is a monthly total, not a dated occurrence.

The component was seeded with **Per Occurrence** ticked *and* a quantity
labelled "Days", which are two different ways to record the same thing: a row
dated 5 September claiming 3 days is a contradiction nothing validates. The
supervisor records how many Fridays the employee worked in the month, one row
per employee, so the date is dropped.

Overtime keeps ``needs_date`` — hours worked on a given day is exactly what
that flag is for, and its entries all carry one.

The catalog is loaded ``noupdate="1"`` so an admin's edits to rates survive an
upgrade (CLAUDE.md gotcha #2): correcting the seeded ``<record>`` fixes fresh
installs and does nothing to an existing database. Hence this script — a
one-time correction rather than a ``<function>`` that would re-impose the value
on every upgrade and overwrite a deliberate choice made later in the UI.

Any Friday entries already recorded keep the date they have; it is ignored
from here on, and the column simply stops being asked for. (There were none
anywhere when this was written.)
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
