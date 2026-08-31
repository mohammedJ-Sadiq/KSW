from . import models


def _mark_company_partners_as_customers(env):
    """Give every company's own partner the customer role.

    ``ksw.fleet.vehicle.client_id`` (and ``ksw.workshop.request.client_id``)
    default to ``env.company.partner_id`` — the company's own fleet is just
    one client among others — while restricting the picker to
    ``customer_rank > 0``. Without this stamp the default value would fall
    outside its own domain on every new record.

    Idempotent: only touches partners whose rank is still 0.
    """
    partners = env['res.company'].sudo().search([]).partner_id
    todo = partners.filtered(lambda p: not p.customer_rank)
    if todo:
        todo.sudo().write({'customer_rank': 1})


def _post_init_hook(env):
    _mark_company_partners_as_customers(env)
