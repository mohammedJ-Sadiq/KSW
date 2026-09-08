from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # The inverse of ksw.workshop.client.partner_id. It exists so the Client
    # pickers can express "registered with the workshop" as a domain, without
    # a flag column on the party master. A One2many adds no column, so none of
    # the stored-field traps on res.partner/hr.employee apply here.
    #
    # The domain form matters: write it as
    #   [('x_workshop_client_ids.active', '=', True)]
    # and NOT as [('x_workshop_client_ids', '!=', False)]. The latter tests the
    # relation directly and never applies the comodel's active filter, so
    # archiving a registration leaves its client in every picker — verified on
    # odoo_dev, and silent, since the domain still looks right.
    x_workshop_client_ids = fields.One2many(
        'ksw.workshop.client', 'partner_id', string='Workshop Client Registration')
