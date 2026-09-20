from odoo import fields, models


class ItConsumableCategory(models.Model):
    _name = 'it.consumable.category'
    _description = 'IT Consumable Category'
    _order = 'sequence, name'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    color = fields.Integer(string='Color')
    icon = fields.Char(help="Font Awesome icon class, e.g. fa-tint")
    active = fields.Boolean(default=True)
    consumable_count = fields.Integer(compute='_compute_consumable_count')
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    _name_company_uniq = models.Constraint(
        'unique(name, company_id)',
        'A category with this name already exists.',
    )

    def _compute_consumable_count(self):
        counts = self.env['it.consumable']._read_group(
            [('category_id', 'in', self.ids)], ['category_id'], ['__count'],
        )
        count_map = {category.id: count for category, count in counts}
        for category in self:
            category.consumable_count = count_map.get(category.id, 0)

    def action_view_consumables(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.name,
            'res_model': 'it.consumable',
            'view_mode': 'list,form',
            'domain': [('category_id', '=', self.id)],
            'context': {'default_category_id': self.id},
        }
