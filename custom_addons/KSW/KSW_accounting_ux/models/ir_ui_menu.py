from odoo import api, models


class IrUiMenu(models.Model):
    _inherit = 'ir.ui.menu'

    @api.model
    def _ksw_place_menus(self, layout):
        """Re-home other modules' menus from one table (views/menus.xml).

        ``layout`` rows are ``(menu_xmlid, parent_xmlid, sequence)`` with an
        optional 4th item, a list of group xmlids that replaces the menu's own.

        A ``write()`` rather than ``<menuitem>``/``<record>`` overrides: those
        reset ``parent_id``/``name`` when not repeated (Pitfalls #1) and are
        skipped on upgrade when the target was created ``noupdate`` (#2).
        Unknown xmlids raise: every module named here is a dependency.
        """
        for row in layout:
            menu_xmlid, parent_xmlid, sequence = row[:3]
            vals = {'parent_id': self.env.ref(parent_xmlid).id, 'sequence': sequence}
            if len(row) > 3:
                vals['group_ids'] = [(6, 0, [self.env.ref(g).id for g in row[3]])]
            self.env.ref(menu_xmlid).write(vals)
        return True

    @api.model
    def _ksw_set_arabic_names(self, names):
        """Arabic labels for other modules' menus: ``{menu_xmlid: label}``.

        Many OCA menus ship no Arabic, and a few core/OCA ones mislead
        ("Closing" as "closing in progress", "Open Items" as "payments
        due"). Writing in the ar_001 context changes only that language, and
        a later translation load never overwrites an existing value.
        """
        if not self.env['res.lang']._lang_get('ar_001'):
            return True
        for xmlid, label in names.items():
            self.env.ref(xmlid).with_context(lang='ar_001').write({'name': label})
        return True
