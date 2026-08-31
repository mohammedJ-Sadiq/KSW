"""19.0.3.4.0 — Breakfast, Lunch and Dinner become one Meals component.

Three components meant three batches to open, three submissions to make and
three lines on the register for what a supervisor thinks of as one thing:
this month's meals. They never differed in anything but the rate, which is
what a component *option* is for.

This moves the history over rather than leaving it split:

* every entry on an old meal component keeps its quantity and its amount and
  gains the matching option (Breakfast / Lunch / Dinner);
* the batches merge — one Meals batch per department and month, the earliest
  of the three kept so its number and its chatter survive;
* the old components are archived, not deleted, so anything that still
  points at them by id resolves.

Deliberately SQL for the re-pointing. The entry constraint requires an
option that belongs to the entry's own component, and an ORM write can only
satisfy it after *both* the batch and the entry have moved — which is one
flush too late. The values written here are ids that already exist, and the
recompute at the end puts the ORM back in charge.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

# old component xml id -> the option that replaces it
REPLACEMENTS = {
    'pay_component_meal_breakfast': 'pay_option_meal_breakfast',
    'pay_component_meal_lunch': 'pay_option_meal_lunch',
    'pay_component_meal_dinner': 'pay_option_meal_dinner',
}


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})

    meals = env.ref('KSW_commissions.pay_component_meals',
                    raise_if_not_found=False)
    if not meals:
        return

    options = {}
    for component_xmlid, option_xmlid in REPLACEMENTS.items():
        component = env.ref('KSW_commissions.%s' % component_xmlid,
                            raise_if_not_found=False)
        option = env.ref('KSW_commissions.%s' % option_xmlid,
                         raise_if_not_found=False)
        if component and option:
            options[component.id] = option
            # The seeded option rate is only a default; what this database
            # actually pays is whatever the old component says, edits to it
            # included (its data block is noupdate, so an admin's rate has
            # been surviving upgrades since it was typed).
            if component.rate and component.rate != option.rate:
                option.write({'rate': component.rate})
    if not options:
        return

    Batch = env['ksw.pay.batch'].sudo()
    old_batches = Batch.with_context(active_test=False).search(
        [('component_id', 'in', list(options))], order='id')

    # Group by what a Meals batch covers: one month, one department (or one
    # site). Whatever the three meal batches were, they collapse into one.
    groups = {}
    for batch in old_batches:
        key = (batch.period, batch.department_id.id, batch.site_id.id)
        groups.setdefault(key, Batch)
        groups[key] |= batch

    moved_entries = merged_batches = 0
    for (period, department_id, site_id), batches in groups.items():
        keeper = Batch.search([
            ('component_id', '=', meals.id),
            ('period', '=', period),
            ('department_id', '=', department_id or False),
            ('site_id', '=', site_id or False),
        ], limit=1) or batches[0]

        for batch in batches:
            option = options[batch.component_id.id]
            entry_ids = batch.entry_ids.ids
            if entry_ids:
                cr.execute("""
                    UPDATE ksw_pay_entry
                       SET batch_id = %s, component_id = %s, option_id = %s
                     WHERE id IN %s
                """, (keeper.id, meals.id, option.id, tuple(entry_ids)))
                moved_entries += len(entry_ids)

        cr.execute(
            "UPDATE ksw_pay_batch SET component_id = %s WHERE id = %s",
            (meals.id, keeper.id))

        # Everything is now on the keeper; the rest carried nothing but a
        # component that no longer exists as such.
        emptied = batches - keeper
        if emptied:
            merged_batches += len(emptied)
            env.invalidate_all()
            emptied.unlink()

    # Recurring entries — the same substitution, one row at a time.
    cr.execute("SELECT id, component_id FROM ksw_pay_recurring "
               "WHERE component_id IN %s", (tuple(options),))
    recurring = cr.fetchall()
    for rec_id, component_id in recurring:
        cr.execute(
            "UPDATE ksw_pay_recurring SET component_id = %s, option_id = %s "
            "WHERE id = %s",
            (meals.id, options[component_id].id, rec_id))

    env.invalidate_all()

    # Hand the moved rows back to the ORM so the stored amounts are
    # re-derived from the option's rate (the same figures — the rates are
    # carried over unchanged — but computed, not asserted).
    entries = env['ksw.pay.entry'].sudo().search(
        [('component_id', '=', meals.id)])
    entries.modified(['component_id', 'option_id'])
    keepers = entries.mapped('batch_id')
    keepers.modified(['entry_ids'])
    env.flush_all()

    for component_id in options:
        env['ksw.pay.component'].browse(component_id).sudo().write(
            {'active': False})

    # The Settings meal prices now edit the option records. Drop the old
    # parameters rather than leave three numbers behind that look
    # authoritative and are read by nothing.
    env['ir.config_parameter'].sudo().search([
        ('key', 'in', ['KSW_commissions.meal_breakfast_price',
                       'KSW_commissions.meal_lunch_price',
                       'KSW_commissions.meal_dinner_price'])
    ]).unlink()

    _logger.info(
        "19.0.3.4.0: %s meal entr(ies) moved onto the Meals component, "
        "%s batch(es) merged away, %s recurring entr(ies) re-pointed; "
        "the three old meal components are archived.",
        moved_entries, merged_batches, len(recurring))
