from odoo import api, fields, models


class KswWorkshopReportWizard(models.TransientModel):
    """Spare Part History reads stock items here, not the pass-through list.

    With the bridge a repair's parts are product.product lines (product_id);
    part_id is empty on them. Only the item hooks change — the grouping,
    figures and layout stay KSW_workshop's.
    """
    _inherit = 'ksw.workshop.report.wizard'

    product_id = fields.Many2one(
        'product.product', string='Spare Part (Stock Item)',
        help="Leave empty for a ranking of every part issued in the period.")

    @api.onchange('product_id')
    def _onchange_product_id(self):
        for wizard in self:
            if wizard.product_id:
                wizard._apply_part_choice()

    def _part_group_field(self):
        return 'product_id'

    def _part_line_part_domain(self):
        if self.product_id:
            return [('product_id', '=', self.product_id.id)]
        if self.part_id:
            return super()._part_line_part_domain()
        return ['|', ('product_id', '!=', False), ('part_id', '!=', False)]

    def _line_part(self, line):
        if line.product_id:
            product = line.product_id
            return product, product.default_code or '', product.name or ''
        return super()._line_part(line)

    def _part_label(self):
        if not self.product_id:
            return super()._part_label()
        return self.product_id.display_name

    def _part_search_text(self):
        if self.product_id:
            return self.product_id.name or ''
        return super()._part_search_text()

    def _part_reference_kpis(self):
        product = self.product_id
        if not product:
            return super()._part_reference_kpis()
        product = product.sudo()
        return [
            (self.env._('Average Cost'), self._num(product.standard_price)),
            (self.env._('On Hand'), '%g' % product.qty_available),
        ]
