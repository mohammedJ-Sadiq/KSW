from odoo import api, fields, models

# Trip sizes the fleet actually runs: 54,320 of 61,834 trailer notes in 2026
# are 32 m³, and 2,860 of 3,026 Isuzu notes are 6 m³.
_TRIP_UNITS = (('Trip (32 m³)', 32.0), ('Trip (6 m³)', 6.0))


def _reference_root(uom):
    while uom.relative_uom_id:
        uom = uom.relative_uom_id
    return uom


class UomUom(models.Model):
    _inherit = 'uom.uom'

    def _ksw_converts_to(self, other):
        """Same reference chain, so a quantity in one is meaningful in the other."""
        self.ensure_one()
        return bool(other) and _reference_root(self) == _reference_root(other)

    def _ksw_qty(self, qty, to_unit):
        """Exact conversion. Core's `_compute_quantity` rounds to the 'Product
        Unit' precision, which turns a 30 m³ load into 0.94 of a 32 m³ trip
        and makes the printed quantity disagree with the amount."""
        self.ensure_one()
        if not to_unit or to_unit == self:
            return qty
        return qty * self.factor / to_unit.factor

    def _ksw_price(self, price, to_unit):
        self.ensure_one()
        if not to_unit or to_unit == self:
            return price
        return price * to_unit.factor / self.factor

    @api.model
    def _ksw_ensure_trip_units(self):
        """A trip as a real unit of m³, so showing an invoice in trips is
        Odoo's own conversion. Re-run on every upgrade; creates only what is
        missing. Keyed on the active m³, because the one the BAS products use
        was created by the GL import and has no external id."""
        cubic = self.search([('name', '=', 'm³'), ('active', '=', True)], limit=1) \
            or self.env.ref('uom.product_uom_cubic_meter', raise_if_not_found=False)
        if not cubic:
            return
        for name, size in _TRIP_UNITS:
            if not self.with_context(active_test=False).search(
                    [('name', '=', name), ('relative_uom_id', '=', cubic.id)], limit=1):
                self.create({'name': name, 'relative_factor': size,
                             'relative_uom_id': cubic.id})


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    # The product sections the delivery-note invoice is built from, and the
    # unit each one is printed in. The note lines underneath always stay in
    # the product's own unit, so the money never goes through a conversion.
    x_water_section = fields.Boolean(copy=False)
    x_display_uom_id = fields.Many2one('uom.uom', string='Print In', copy=False)


class AccountMove(models.Model):
    _inherit = 'account.move'

    x_water_summary = fields.Boolean(
        string='Print One Line per Product', copy=False,
        help='The printed invoice shows one line per product. The delivery '
             'notes stay on the invoice underneath; untick to print each one.',
    )
    x_has_water_sections = fields.Boolean(compute='_compute_has_water_sections')

    @api.depends('invoice_line_ids.x_water_section')
    def _compute_has_water_sections(self):
        for move in self:
            move.x_has_water_sections = any(move.invoice_line_ids.mapped('x_water_section'))

    def _water_section_lines(self, section):
        return self.invoice_line_ids.filtered(
            lambda l: l.display_type == 'product' and l.parent_id == section)

    def _get_move_lines_to_report(self):
        lines = super()._get_move_lines_to_report()
        if not self.x_water_summary:
            return lines
        return lines.filtered(lambda l: not (
            l.x_water_section or l.parent_id.x_water_section))

    def _water_report_sa_columns(self):
        """Whether the Saudi invoice layout (VAT / excl. / incl. columns) is
        the one printing."""
        view = self.env.ref('l10n_sa.l10n_sa_report_invoice_document', raise_if_not_found=False)
        return bool(view and view.active)

    def _water_summary_rows(self):
        """One printed row per delivery-note section, in its display unit.

        The amount is the sum of the note lines, never re-derived from the
        converted quantity, so the invoice total cannot move. The quantity is
        printed exactly (a 30 m³ load is 0.9375 of a 32 m³ trip), so that
        quantity x unit price still reads true to the halala.
        """
        self.ensure_one()
        rows = []
        for section in self.invoice_line_ids.filtered('x_water_section').sorted('sequence'):
            lines = self._water_section_lines(section)
            if not lines:
                continue
            unit = section.x_display_uom_id or lines[0].product_uom_id
            quantity = sum(l.product_uom_id._ksw_qty(l.quantity, unit) for l in lines)
            prices = {round(l.product_uom_id._ksw_price(l.price_unit, unit), 6) for l in lines}
            subtotal = sum(lines.mapped('price_subtotal'))
            price_unit = prices.pop() if len(prices) == 1 else (subtotal / quantity if quantity else 0.0)
            rows.append({
                'name': lines[0].product_id.display_name,
                'quantity': ('%.4f' % quantity).rstrip('0').rstrip('.'),
                'uom': unit.display_name,
                'price_unit': price_unit,
                'taxes': ', '.join(sorted({t.tax_label for t in lines.tax_ids if t.tax_label})),
                'price_subtotal': subtotal,
                'price_total': sum(lines.mapped('price_total')),
            })
        return rows
